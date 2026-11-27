"""Prepare a full scope replacement on a reader; publish only its guarded point mutations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from itertools import islice
from math import inf

from persistence.authority.vocabulary import RETRY_HORIZON_S
from persistence.core.errors import StoreContention
from persistence.gateway.routing import route_namespace

from ..conversation._store.retirement import RETIRED_PREFIX
from ._revision import ROUTE_FENCE_PREFIX, revision, verified_schema
from ._routes import FENCE_EXPIRY, marker_applies_sql
from ._targets import route_namespace_sql


_PROBE = f"""WITH desired(key, entry) AS (VALUES {{slots}}),
    targets AS MATERIALIZED (
        SELECT key, current.session_key IS NOT NULL AS present, current.entry_json IS NOT entry AS changed,
               {route_namespace_sql('key')} AS ns, CAST(json_extract(entry, '$.session_id') AS TEXT) AS sid
        FROM desired LEFT JOIN gateway_routing current ON current.scope = :scope AND current.session_key = key)
    SELECT t.key, t.present, t.changed,
           m.key IS NOT NULL AND {marker_applies_sql('t.ns')}, f.key IS NOT NULL, {FENCE_EXPIRY}, t.sid,
           CASE WHEN f.key IS NOT NULL AND t.ns = 'main' THEN EXISTS(SELECT 1 FROM sessions WHERE id=t.sid) END
    FROM targets t
    LEFT JOIN state_meta m ON m.key = :retired_prefix || t.sid
    LEFT JOIN state_meta f ON f.key = :fence_prefix || t.ns || ':' || t.sid"""
_PUT = """INSERT INTO gateway_routing(scope, session_key, entry_json, updated_at) VALUES (?, ?, ?, ?)
          ON CONFLICT(scope, session_key) DO UPDATE SET entry_json=excluded.entry_json, updated_at=excluded.updated_at"""


def _proposed_rows(conn, scope, desired):
    items = iter(desired.items())
    while batch := tuple(islice(items, 128)):
        params = {'scope': scope, 'fence_prefix': ROUTE_FENCE_PREFIX, 'retired_prefix': RETIRED_PREFIX}
        slots = []
        for index, (key, text) in enumerate(batch):
            params[f'k{index}'], params[f'v{index}'] = key, text
            slots.append(f'(:k{index},:v{index})')
        yield from conn.execute(_PROBE.format(slots=','.join(slots)), params).fetchall()


def scope_rows(conn, scope, columns):
    after = None
    while True:
        where, params = ('', (scope,)) if after is None else (' AND session_key>?', (scope, after))
        page = conn.execute(f'SELECT {columns} FROM gateway_routing WHERE scope=?{where} '
                            'ORDER BY session_key LIMIT 64', params).fetchall()
        yield from page
        if len(page) < 64:
            return
        after = page[-1][0]


def load_scope(holder, scope):
    names = ('scope:' + scope,)
    deadline = time.monotonic() + holder._WRITE_PATIENCE_S
    with holder.independent_read_connection() as conn:
        while True:
            schema = verified_schema(conn)
            before = revision(conn, names)
            rows = dict(scope_rows(conn, scope, 'session_key,entry_json'))
            if before == revision(conn, names) and conn.execute('PRAGMA main.schema_version').fetchone()[0] == schema:
                return rows
            if time.monotonic() >= deadline:
                raise StoreContention('routing scope kept changing during load')


@dataclass
class _Fences:
    """An unchanged fence row can change its verdict only at a clock boundary or a main id's
    insertion/deletion. SQLite compares numeric retired_at cells to the numeric cutoff; text
    and NULL stamps never expire. Bound the cutoff itself to preserve SQLite's strict '<'."""

    after: float = -inf
    through: float = inf

    def retired(self, stamp, owned, cutoff):
        if isinstance(stamp, (int, float)):
            if stamp < cutoff:
                self.after = max(self.after, stamp)
                return False
            self.through = min(self.through, stamp)
        return not owned

    def valid(self, cutoff):
        return self.after < cutoff <= self.through


@dataclass(frozen=True)
class _Delta:
    revision: tuple
    names: tuple[str, ...]
    schema: int
    scope: str
    drops: set[str]
    puts: dict[str, str]
    retired: set[str]
    fences: _Fences

    @classmethod
    def prepare(cls, conn, scope, desired, names):
        schema = verified_schema(conn)
        before = revision(conn, names)
        drops = {key for (key,) in scope_rows(conn, scope, 'session_key') if key not in desired}
        puts, retired, fences = {}, set(), _Fences()
        cutoff = time.time() - RETRY_HORIZON_S
        for key, present, changed, marked, fenced, stamp, _, owned in _proposed_rows(conn, scope, desired):
            if marked or (fenced and fences.retired(stamp, owned, cutoff)):
                retired.add(key)
                if present:
                    drops.add(key)
            elif changed:
                puts[key] = desired[key]
        return cls(before, names, schema, scope, drops, puts, retired, fences)

    def commit(self, conn):
        now = time.time()
        if (revision(conn, self.names) != self.revision
                or conn.execute('PRAGMA main.schema_version').fetchone()[0] != self.schema
                or not self.fences.valid(now - RETRY_HORIZON_S)):
            return False
        conn.executemany('DELETE FROM gateway_routing WHERE scope=? AND session_key=?',
                         ((self.scope, key) for key in self.drops))
        conn.executemany(_PUT, ((self.scope, key, text, now) for key, text in self.puts.items()))
        return True


def replace_scope(holder, entries, scope):
    """Replace one scope after validating membership, relevant fences, ownership and expiry.
    Messages and ordinary session activity never invalidate this preparation."""
    desired = {key: text for key, text in entries.items() if key and text}
    names = tuple({'scope:' + scope, 'legacy', *('ns:' + route_namespace(key) for key in desired)})
    deadline = time.monotonic() + holder._WRITE_PATIENCE_S
    while True:
        with holder.independent_read_connection() as conn:
            delta = _Delta.prepare(conn, scope, desired, names)
        if holder._execute_write(delta.commit):
            return delta.retired
        if time.monotonic() >= deadline:
            raise StoreContention('routing scope kept changing during full save')
