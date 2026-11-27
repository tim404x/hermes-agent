"""One persistent revision per routing scope and one for fences, including direct SQL writers.

SQLite REPLACE need not fire a DELETE trigger for a displaced row. A primary-key collision
is in the destination scope; an explicit rowid can displace a different scope (or marker),
so inspect that one row before it disappears. Never attach triggers to messages or activity.
"""

from ..conversation._store.retirement import RETIRED_PREFIX

ROUTE_FENCE_PREFIX = 'gateway.retired_route.v1.'
_BUMP = "INSERT INTO gateway_routing_revision(name, revision) {} ON CONFLICT(name) DO UPDATE SET revision=revision+1;"


def _fence_key(row):
    return f"({row}.key GLOB '{RETIRED_PREFIX}*' OR {row}.key GLOB '{ROUTE_FENCE_PREFIX}*')"


def _triggers():
    new, old = _fence_key('new'), _fence_key('old')
    displaced = f"EXISTS (SELECT 1 FROM state_meta victim WHERE victim.rowid=new.rowid AND {_fence_key('victim')})"
    for event, affected in [('INSERT', f'{new} OR {displaced}'), ('DELETE', old),
                            ('UPDATE', f'{new} OR {old} OR {displaced}')]:
        yield (f'state_meta_routing_v1_{event}', f'BEFORE {event} ON state_meta WHEN {affected}',
               _BUMP.format("VALUES ('fences',1)"))
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
REVISION_TRIGGERS_SQL = ''.join(f'CREATE TRIGGER IF NOT EXISTS {name}\n{event}\nBEGIN\n{body}\nEND;\n'
                                for name, event, body in _DEFINITIONS)
