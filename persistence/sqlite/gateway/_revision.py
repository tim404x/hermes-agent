"""Scoped route and retirement witnesses, including direct SQL writers and older gateways.

SQLite REPLACE need not fire a DELETE trigger for a displaced row. A primary-key collision
is in the destination scope; an explicit rowid can displace a different scope (or marker),
so inspect that one row before it disappears. Never attach triggers to messages or activity.
"""

import json

from persistence.core.errors import DeclarativeDrift
from ..conversation._store.retirement import RETIRED_PREFIX

ROUTE_FENCE_PREFIX = 'gateway.retired_route.v1.'
_BUMP = "INSERT INTO gateway_routing_revision(name, revision) {} ON CONFLICT(name) DO UPDATE SET revision=revision+1;"
_WINDOW = 32
EXPIRING_MARKER = (f"(key GLOB '{ROUTE_FENCE_PREFIX}*' OR key GLOB '{RETIRED_PREFIX}*') AND "
                   "CASE WHEN json_valid(value) THEN json_extract(value,'$.expires') END")
EXPIRY_STAMP = "CASE WHEN json_valid(value) THEN json_extract(value,'$.retired_at') END"
EXPIRY_INDEX = f'CREATE INDEX idx_routing_marker_expiry ON state_meta({EXPIRY_STAMP},key) WHERE {EXPIRING_MARKER}'


def _fence_key(row):
    return f"({row}.key GLOB '{RETIRED_PREFIX}*' OR {row}.key GLOB '{ROUTE_FENCE_PREFIX}*')"


def _fence_bump(facts):
    """A bounded witness for changes *during* a read. Never a source of retirement verdicts.
    A secondary fence can also write a main fence for the same id. That must not force a gateway
    saving other ids to start again. On window overflow, use the ordinary fresh-read retry."""
    return _BUMP.format(f'SELECT DISTINCT name,1 FROM ({facts}) WHERE name IS NOT NULL') + f'''
INSERT INTO gateway_routing_fence_window(name,slot,revision,targets)
SELECT changes.name, r.revision % {_WINDOW}, r.revision, json_group_array(DISTINCT changes.sid)
FROM ({facts}) changes JOIN gateway_routing_revision r ON r.name=changes.name GROUP BY changes.name
ON CONFLICT(name,slot) DO UPDATE SET revision=excluded.revision, targets=excluded.targets;'''


def _triggers():
    new, old = _fence_key('new'), _fence_key('old')
    displaced = f"EXISTS (SELECT 1 FROM state_meta victim WHERE victim.rowid=new.rowid AND {_fence_key('victim')})"
    for event, affected in [('INSERT', f'{new} OR {displaced}'), ('DELETE', old),
                            ('UPDATE', f'{new} OR {old} OR {displaced}')]:
        facts = []
        if event != 'INSERT':
            facts.append('SELECT old.key AS key, old.value AS value')
        if event != 'DELETE':
            facts.extend(['SELECT new.key AS key, new.value AS value',
                          'SELECT key,value FROM state_meta WHERE rowid=new.rowid OR key=new.key'])
        tail = f"substr(CAST(key AS BLOB), {len(ROUTE_FENCE_PREFIX) + 1})"
        name = (f"CASE WHEN key GLOB '{ROUTE_FENCE_PREFIX}*' THEN 'ns:' || "
                f"CAST(substr({tail},1,instr({tail},x'3a')-1) AS TEXT) "
                f"WHEN key GLOB '{RETIRED_PREFIX}*' THEN CASE WHEN json_valid(value) THEN "
                "CASE WHEN json_extract(value,'$.own') IS NOT NULL THEN 'ns:main' ELSE 'legacy' END ELSE 'legacy' END END")
        sid = (f"CASE WHEN key GLOB '{ROUTE_FENCE_PREFIX}*' THEN CAST(substr({tail},instr({tail},x'3a')+1) AS TEXT) "
               f"ELSE CAST(substr(CAST(key AS BLOB),{len(RETIRED_PREFIX) + 1}) AS TEXT) END")
        changes = f"SELECT {name} AS name,{sid} AS sid FROM ({' UNION ALL '.join(facts)})"
        yield (f'state_meta_routing_v1_{event}', f'BEFORE {event} ON state_meta WHEN {affected}',
               _BUMP.format("VALUES ('fences',1)") + '\n' + _fence_bump(changes))
    for event, clause in [('INSERT', 'INSERT'), ('DELETE', 'DELETE'), ('UPDATE', 'UPDATE OF id, rowid, oid, _rowid_')]:
        ids = ['old.id'] if event != 'INSERT' else []
        if event != 'DELETE':
            ids += ['new.id', '(SELECT id FROM sessions WHERE rowid=new.rowid)']
        keys = ','.join(f"'{ROUTE_FENCE_PREFIX}main:' || {sid}" for sid in ids)
        yield (f'sessions_route_owner_{event}', f'BEFORE {clause} ON sessions WHEN EXISTS '
               f'(SELECT 1 FROM state_meta WHERE key IN ({keys}))',
               _fence_bump(' UNION ALL '.join(f"SELECT 'ns:main' AS name,{sid} AS sid" for sid in ids)))
    for event, row in [('INSERT', 'new'), ('DELETE', 'old'), ('UPDATE', 'new')]:
        body = _BUMP.format(f"VALUES ('scope:' || {row}.scope,1)")
        if event == 'UPDATE':
            body += '\n' + _BUMP.format("SELECT 'scope:' || old.scope,1 WHERE old.scope IS NOT new.scope")
        yield f'gateway_routing_v1_{event}', f'AFTER {event} ON gateway_routing', body
    for event, clause in [('INSERT', 'INSERT'), ('UPDATE', 'UPDATE OF rowid, oid, _rowid_')]:
        yield (f'gateway_routing_rowid_v1_{event}', f'BEFORE {clause} ON gateway_routing', _BUMP.format(
            "SELECT 'scope:' || scope,1 FROM gateway_routing WHERE rowid=new.rowid AND scope IS NOT new.scope"))


_DEFINITIONS = tuple(_triggers())
TRIGGERS = tuple(name for name, _, _ in _DEFINITIONS)
_EXPECTED = {name: f'CREATE TRIGGER {name}\n{event}\nBEGIN\n{body}\nEND' for name, event, body in _DEFINITIONS}
REVISION_TRIGGERS_SQL = '''CREATE TABLE IF NOT EXISTS gateway_routing_revision (
    name TEXT PRIMARY KEY, revision INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gateway_routing_fence_window (
    name TEXT NOT NULL, slot INTEGER NOT NULL, revision INTEGER NOT NULL, targets TEXT NOT NULL,
    PRIMARY KEY(name,slot)
) WITHOUT ROWID;
''' + ''.join(f'DROP TRIGGER IF EXISTS {name};\n' + definition.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1)
              + ';\n' for name, definition in _EXPECTED.items())


def verified_schema(conn):
    schema = conn.execute('PRAGMA main.schema_version').fetchone()[0]
    slots = ','.join('?' for _ in TRIGGERS)
    live = dict(conn.execute(f"SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name IN ({slots})", TRIGGERS))
    if live != _EXPECTED:
        raise DeclarativeDrift('routing revision triggers missing or displaced; reopen state.db')
    return schema


def revision(conn, names):
    slots = ','.join('?' for _ in names)
    return tuple(conn.execute(f'SELECT name,revision FROM gateway_routing_revision WHERE name IN ({slots}) ORDER BY name', names))


def fences_changed(conn, names, before, wanted):
    after = revision(conn, names)
    if before == after:
        return False
    old = dict(before)
    for name, last in after:
        first = old.pop(name, 0)
        if last == first:
            continue
        if not 0 < last - first <= _WINDOW:
            return True
        rows = conn.execute('SELECT targets FROM gateway_routing_fence_window WHERE name=? AND revision>? AND revision<=?',
                            (name, first, last)).fetchall()
        if len(rows) != last - first or any(not wanted.isdisjoint(json.loads(row[0])) for row in rows):
            return True
    return bool(old)
