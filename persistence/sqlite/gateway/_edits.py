"""Gateway generation writes. The gateway owns the edit set, never the whole database scope."""

import json
import time
from dataclasses import dataclass
from itertools import islice
from uuid import uuid4

from persistence.authority.vocabulary import RETRY_HORIZON_S
from persistence.core.errors import StoreContention
from persistence.gateway.routing import route_namespace

from ..conversation._store.retirement import RETIRED_PREFIX
from ._delta import _Fences, _PUT
from ._revision import ROUTE_FENCE_PREFIX, fences_changed, revision, verified_schema
from ._routes import FENCE_EXPIRY, ROUTE_RETIRED_SQL, route_retired_params
from ._targets import stored_target


def _meta(conn, key):
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def _mirror_owner(conn, scope, create=False):
    key = 'gateway.routing_mirror.owner:' + scope
    owner = _meta(conn, key)
    if not owner and create:
        owner = uuid4().hex
        conn.execute('INSERT INTO state_meta VALUES (?,?)', (key, owner))
    return owner


def _marker_pages(conn, prefix, columns):
    after, end = prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)
    comparison = '>='
    while True:
        page = conn.execute(f'SELECT f.key,{columns} FROM state_meta f WHERE f.key{comparison}? AND f.key<? '
                            'ORDER BY f.key LIMIT 64', (after, end)).fetchall()
        yield from page
        if len(page) < 64:
            return
        after, comparison = page[-1][0], '>'


def _retired(conn, wanted, namespaces, cutoff):
    facts, fences = set(), _Fences()
    if not wanted:
        return facts, fences
    for key, own in _marker_pages(conn, RETIRED_PREFIX, "CASE WHEN json_valid(value) THEN json_extract(value,'$.own') END"):
        sid = key[len(RETIRED_PREFIX):]
        if sid in wanted:
            facts.add((None if own is None else 'main', sid))
    for namespace in namespaces:
        prefix = f'{ROUTE_FENCE_PREFIX}{namespace}:'
        owner = ('EXISTS(SELECT 1 FROM sessions WHERE id=CAST(substr(CAST(f.key AS BLOB),'
                 f'{len(prefix.encode()) + 1}) AS TEXT))') if namespace == 'main' else '0'
        for key, expires, owned in _marker_pages(conn, prefix, f'{FENCE_EXPIRY},{owner}'):
            sid = key[len(prefix):]
            if sid in wanted and fences.retired(expires, owned, cutoff):
                facts.add((namespace, sid))
    return facts, fences


@dataclass(frozen=True)
class _LiveGeneration:
    scope: str
    conn: object
    schema: int
    names: tuple
    before: tuple
    wanted: set
    targets: dict
    clock: _Fences


def _retired_drops(conn, scope, retired, targets, prior):
    drops = []
    for key in retired:
        text = _current(conn, scope, key)
        if text is not None and (_id_text(conn, stored_target(text)) == targets[key] or _matches(text, prior.get(key))):
            drops.append((scope, key))
    return drops


def _write_edits(conn, scope, puts, deletes, repairs, retired, stale, now):
    conn.executemany('DELETE FROM gateway_routing WHERE scope=? AND session_key=?', ((scope, key) for key in deletes))
    conn.executemany('DELETE FROM gateway_routing WHERE scope=? AND session_key=?', stale)
    conn.executemany(_PUT, ((scope, key, text, now) for key, text in puts.items() if key not in retired and key not in repairs))
    conn.executemany('UPDATE gateway_routing SET updated_at=?,entry_json=? WHERE scope=? AND session_key=? AND entry_json=?',
                     ((now, puts[key], scope, key, repairs[key]) for key in repairs if key in puts and key not in retired))


def _id_text(conn, sid):
    if sid is None or isinstance(sid, str):
        return sid
    return conn.execute("SELECT CAST(json_extract(?,'$') AS TEXT)", (json.dumps(sid),)).fetchone()[0]


def _text_targets(holder, entries):
    targets = dict(entries)
    legacy = [key for key, sid in targets.items() if not isinstance(sid, str)]
    if legacy:
        with holder.independent_read_connection() as reader:
            for key in legacy:
                sid = _id_text(reader, targets[key])
                if sid is None:
                    del targets[key]
                else:
                    targets[key] = sid
    return targets


def save_edits(holder, scope, puts, deletes, targets, mirror, repairs, prior, previous):
    targets = _text_targets(holder, targets)
    wanted = set(targets.values())
    namespaces = {route_namespace(key) for key in targets}
    names = ('legacy', *('ns:' + namespace for namespace in namespaces))
    deadline = time.monotonic() + holder._WRITE_PATIENCE_S

    def publish(conn, schema, clock, retired, stale):
        _write_edits(conn, scope, puts, deletes, repairs, retired, stale, time.time())
        proof = None if retired else _LiveGeneration(scope, conn, schema, names, revision(conn, names), wanted, targets, clock)
        return (retired, _mirror_owner(conn, scope, create=True) if mirror else None), proof

    if previous and previous.scope == scope:
        additions = tuple(islice(((RETIRED_PREFIX + sid, f'{ROUTE_FENCE_PREFIX}{route_namespace(key)}:{sid}')
                                  for key, sid in targets.items() if previous.targets.get(key) != sid), 17))
        if len(additions) <= 16:
            def warm(conn):
                if (previous.conn is not conn or conn.execute('PRAGMA main.schema_version').fetchone()[0] != previous.schema
                        or not previous.clock.valid(time.time() - RETRY_HORIZON_S)
                        or fences_changed(conn, previous.names, previous.before, previous.wanted)
                        or any(conn.execute('SELECT 1 FROM state_meta WHERE key IN (?,?)', keys).fetchone() for keys in additions)):
                    return None
                return publish(conn, previous.schema, previous.clock, set(), ())

            if result := holder._execute_write(warm):
                return result
    while True:
        with holder.independent_read_connection() as reader:
            schema = verified_schema(reader)
            before = revision(reader, names)
            facts, clock = _retired(reader, wanted, namespaces, time.time() - RETRY_HORIZON_S)
            retired = {key for key, sid in targets.items() if (None, sid) in facts or
                       (route_namespace(key), sid) in facts} if facts else set()
            dense = len(retired) > 16
            scope_name = ('scope:' + scope,)
            snapshot = revision(reader, scope_name) if dense else ()
            stale = _retired_drops(reader, scope, retired, targets, prior) if dense else ()
        def commit(conn):
            now = time.time()
            if (fences_changed(conn, names, before, wanted) or not clock.valid(now - RETRY_HORIZON_S)
                    or (dense and revision(conn, scope_name) != snapshot)
                    or conn.execute('PRAGMA main.schema_version').fetchone()[0] != schema):
                return None
            drops = stale if dense else _retired_drops(conn, scope, retired, targets, prior)
            return publish(conn, schema, clock, retired, drops)

        result = holder._execute_write(commit)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            raise StoreContention('relevant routing fences kept changing during save')


def _current(conn, scope, key):
    row = conn.execute('SELECT entry_json FROM gateway_routing WHERE scope=? AND session_key=?', (scope, key)).fetchone()
    return row[0] if row else None


def _matches(current, expected):
    if current == expected:
        return True
    if current is None or expected is None:
        return False
    try:
        return json.dumps(json.loads(current), sort_keys=True) == json.dumps(json.loads(expected), sort_keys=True)
    except (ValueError, TypeError):
        return False


def reconcile(conn, scope, digest, owner, missing, pending):
    projection = owner is not None and owner == _mirror_owner(conn, scope)
    if projection and not pending:
        return {}
    consumed = 'gateway.routing_mirror.consumed:' + json.dumps([scope, digest])
    if _meta(conn, consumed) is not None:
        return {}
    resolved, now = {}, time.time()
    for key in (pending.keys() if projection else missing.keys() | pending.keys()):
        current = _current(conn, scope, key)
        before, after = pending.get(key, (None, missing.get(key)))
        if not _matches(current, before) and (current is not None or projection or key not in missing):
            resolved[key] = current
            continue
        if after is not None and conn.execute('SELECT ' + ROUTE_RETIRED_SQL,
                                              route_retired_params(key, after, now)).fetchone()[0]:
            after = None
        if after is None:
            if current is not None:
                conn.execute('DELETE FROM gateway_routing WHERE scope=? AND session_key=?', (scope, key))
        elif after != current:
            conn.execute(_PUT, (scope, key, after, now))
        resolved[key] = after
    conn.execute('INSERT INTO state_meta VALUES (?,?)', (consumed, '1'))
    return resolved
