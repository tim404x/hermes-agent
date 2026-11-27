"""Operational contracts for routing when readers, schema versions and writers overlap."""

import json
import random
import sqlite3
from contextlib import closing, contextmanager
from unittest.mock import patch

import pytest

from persistence.core.errors import PermanentStoreError
from persistence.sqlite._state.readpool import _READ_POOL_MAX
from persistence.sqlite.conversation._store.retirement import retire_routes
from persistence.sqlite.gateway._routes import route_fence_key
from persistence_assembly import open_persistence_reader


def test_full_reader_pool_cannot_starve_routing_commit(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    holder = store.holder()
    readers = [holder._checkout_read_conn() for _ in range(_READ_POOL_MAX)]
    try:
        assert all(readers)
        monkeypatch.setattr(holder, '_WRITE_PATIENCE_S', 0.05)
        assert not store.routing.replace_routing_entries({'key': '{}'})
        assert store.routing.load_routing_entries() == {'key': '{}'}
    finally:
        for reader in filter(None, readers):
            holder._close_read_conn(reader)


def test_unrelated_namespace_cannot_invalidate_every_preparation(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    holder = store.holder()
    write = holder._execute_write
    monkeypatch.setattr(holder, '_WRITE_PATIENCE_S', 0.05)

    def competing(fn):
        with closing(sqlite3.connect(holder.db_path)) as peer, peer:
            peer.execute('INSERT OR REPLACE INTO state_meta VALUES (?,?)', (route_fence_key('other', 'target'), '{}'))
        return write(fn)

    with patch.object(holder, '_execute_write', competing):
        assert not store.routing.replace_routing_entries({'agent:ours:key': '{"session_id":"target"}'})


@pytest.mark.parametrize('reader_first', [True, False], ids=['reader-first', 'writer-first'])
def test_reader_first_without_writer_accelerators(backend, scope_a, reader_first):
    store = backend.open(scope_a)
    holder = store.holder()
    path = holder.db_path
    store.routing.upsert_routing_entry('history', '{"session_id":"kept"}')
    holder.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        for name, in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND sql LIKE '%gateway_routing_revision%'").fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute('DROP TABLE IF EXISTS gateway_routing_revision')
        conn.execute('DROP TABLE IF EXISTS gateway_routing_fence_window')
        conn.execute('DROP INDEX IF EXISTS idx_gateway_routing_target')
        conn.execute('DROP INDEX IF EXISTS idx_routing_marker_expiry')
    if reader_first:
        with open_persistence_reader(path, check_drift=True) as reader:
            assert reader.routing.load_routing_entries() == {'history': '{"session_id":"kept"}'}
        with closing(sqlite3.connect(path)) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_schema WHERE name='gateway_routing_revision'").fetchone()
    store.routing.upsert_routing_entry('upgraded', '{"session_id":"extra"}')
    with closing(sqlite3.connect(path)) as peer, peer:
        version = peer.execute('PRAGMA schema_version').fetchone()
        peer.execute("UPDATE gateway_routing SET entry_json='{\"session_id\":\"older-client\"}' WHERE session_key='upgraded'")
    store.holder().close()
    with open_persistence_reader(path, check_drift=True) as reader:
        assert reader.routing.load_routing_entries()['upgraded'] == '{"session_id":"older-client"}'
    store.routing.fence_retired_routes(['older-client'], namespace='main')
    assert store.routing.load_routing_entries() == {'history': '{"session_id":"kept"}'}
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA schema_version').fetchone() == version


def test_rollback_reader_releases_shared_lock_between_batches(backend, scope_a, monkeypatch):
    from persistence.sqlite import _holder
    monkeypatch.setattr(_holder, 'apply_wal_with_fallback', lambda conn, **_: conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0])
    store = backend.open(scope_a)
    holder = store.holder()
    assert holder._conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    independent = holder.independent_read_connection
    pages = []

    class Reader:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *params):
            if sql.startswith('WITH desired'):
                pages.append(True)
                if len(pages) == 2:
                    with closing(sqlite3.connect(holder.db_path, timeout=0)) as peer, peer:
                        peer.execute("INSERT INTO gateway_routing VALUES ('other','competing','{}',0)")
            return self.conn.execute(sql, *params)

        def rollback(self):
            self.conn.rollback()

    @contextmanager
    def instrumented():
        with independent() as conn:
            yield Reader(conn)

    with patch.object(holder, 'independent_read_connection', instrumented):
        store.routing.replace_routing_entries({f'key-{index}': '{}' for index in range(512)})
    assert len(pages) > 2 and store.routing.load_routing_entries(routing_scope='other') == {'competing': '{}'}


def test_rebuilt_table_with_displaced_triggers_refuses_then_repairs(backend, scope_a):
    store = backend.open(scope_a)
    holder = store.holder()
    with closing(sqlite3.connect(holder.db_path)) as peer, peer:
        definition = peer.execute("SELECT sql FROM sqlite_schema WHERE name='gateway_routing'").fetchone()[0]
        peer.execute('ALTER TABLE gateway_routing RENAME TO displaced_routes')
        peer.execute(definition)
        peer.execute("INSERT INTO gateway_routing VALUES ('', 'direct', '{}', 1)")
    with pytest.raises(PermanentStoreError):
        store.routing.replace_routing_entries({})
    path = holder.db_path
    holder.close()
    store.routing.replace_routing_entries({})
    assert store.routing.load_routing_entries() == {}
    with open_persistence_reader(path, check_drift=True) as reader:
        reader.ready()


@pytest.mark.parametrize('shared', [True, False])
def test_retiring_one_target_does_not_visit_unrelated_routes(backend, scope_a, shared):
    store = backend.open(scope_a)
    holder = store.holder()
    holder._write_sql('INSERT INTO gateway_routing VALUES (?,?,?,0)', [
        ('scope', f'agent:main:{index}', json.dumps({'session_id': f's-{index}', 'pin': 'p' * 4096}))
        for index in range(1000)] + [('scope', 'bad', '{'), ('scope', 'legacy', '{ "session_id" : "victim" }')], many=True)
    blocks = []
    holder._conn.set_progress_handler(lambda: blocks.append(1), 128)
    try:
        if shared:
            store.routing.fence_retired_routes(['victim'], namespace='main')
        else:
            holder._execute_write(lambda conn: retire_routes(conn, ['victim']))
    finally:
        holder._conn.set_progress_handler(None, 0)
    rows = store.routing.load_routing_entries(routing_scope='scope')
    assert 'legacy' not in rows and rows['bad'] == '{' and len(rows) == 1001
    assert len(blocks) < 25


def test_live_main_fence_set_does_not_reprobe_all_owners_on_writer(backend, scope_a):
    store = backend.open(scope_a)
    holder = store.holder()
    desired = {f'key-{index}': json.dumps({'session_id': f's-{index}'}) for index in range(1000)}
    holder._write_sql('INSERT INTO gateway_routing VALUES (\'\',?,?,0)', desired.items(), many=True)
    holder._write_sql("INSERT INTO sessions (id,source,started_at) VALUES (?,'cli',0)",
                      [(f's-{index}',) for index in range(1000)], many=True)
    holder._write_sql("INSERT INTO state_meta VALUES (?,'{}')", [(route_fence_key('main', f's-{index}'),) for index in range(1000)], many=True)
    blocks = []
    holder._conn.set_progress_handler(lambda: blocks.append(1), 128)
    try:
        assert not store.routing.replace_routing_entries(desired)
    finally:
        holder._conn.set_progress_handler(None, 0)
    assert len(blocks) < 10


def test_point_retirement_matches_previous_json_decoder(backend, scope_a):
    store = backend.open(scope_a)
    values = {'duplicate': '{"session_id":"old","session_id":"victim"}',
              'keep': '{"session_id":"victim","session_id":"live"}',
              'nan': '{"session_id":"victim","legacy":NaN}', 'bad': 'invalid NaN',
              'utf16': '{"session_id":"victim"}'.encode('utf-16')}
    store.holder()._write_sql("INSERT INTO gateway_routing VALUES ('',?,?,0)", values.items(), many=True)
    store.routing.fence_retired_routes(['victim'], namespace='main')
    assert store.routing.load_routing_entries() == {key: values[key] for key in ('keep', 'bad')}


def test_gateway_retired_set_matches_point_oracle_across_generations(backend, scope_a):
    store = backend.open(scope_a)
    holder, routing = store.holder(), store.routing
    rng = random.Random(82307)
    targets = {f'agent:{name}:{index}': f's-{index}' for name in ('main', 'δ') for index in range(24)}
    payloads = {key: json.dumps({'session_id': sid}) for key, sid in targets.items()}
    first, _ = routing.save_routing_edits(payloads, set(), targets, routing_scope='scope', mirror=False)
    assert not first
    for _ in range(48):
        sid = rng.choice(list(targets.values()))
        marker = rng.choice([route_fence_key('main', sid), route_fence_key('δ', sid), 'gateway.retired_session.v1.' + sid])
        holder._write_sql('INSERT OR REPLACE INTO state_meta VALUES (?,?)',
                          (marker, rng.choice(['{}', '{', '{"own":true}', '{"expires":true,"retired_at":0}'])))
        if rng.randrange(2):
            holder._write_sql("INSERT OR IGNORE INTO sessions(id,source,started_at) VALUES (?,'cli',0)", (sid,))
        expected = {key for key, text in payloads.items() if routing.route_retired(key, text)}
        actual, _ = routing.save_routing_edits({}, set(), targets, routing_scope='scope', mirror=False)
        assert actual == expected


@pytest.mark.parametrize('count', [1, 20])
def test_retired_set_rechecks_rows_inserted_after_reader(backend, scope_a, count):
    store = backend.open(scope_a)
    holder = store.holder()
    targets = {f'agent:other:{index}': 'dead' for index in range(count)}
    store.routing.fence_retired_routes(['dead'], namespace='other')
    execute = holder._execute_write
    attempts = []

    def race(callback):
        if not attempts:
            with closing(sqlite3.connect(holder.db_path)) as peer, peer:
                peer.execute("INSERT INTO gateway_routing VALUES ('scope','agent:other:0','{\"session_id\":\"dead\"}',0)")
            attempts.append(True)
        return execute(callback)

    with patch.object(holder, '_execute_write', race):
        retired, _ = store.routing.save_routing_edits({}, set(), targets, routing_scope='scope', mirror=False)
    assert retired == targets.keys() and not store.routing.load_routing_entries(routing_scope='scope')
