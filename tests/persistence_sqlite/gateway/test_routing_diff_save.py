"""Write cost and retirement semantics of replacing one routing scope."""

import json
import random
import sqlite3
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import pytest

from persistence.authority.vocabulary import RETRY_HORIZON_S
from persistence.core.errors import PermanentStoreError
from persistence.gateway.routing import route_namespace
from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
from persistence.sqlite.gateway._routes import route_fence_key
from persistence.sqlite.gateway._targets import route_namespace_sql


def test_one_changed_route_among_10k_writes_o1_rows_and_holds_writer_briefly(backend, scope_a, monkeypatch):
    from persistence.sqlite import _holder

    store = backend.open(scope_a)
    holder = store.holder()
    entries = {}
    for index in range(10_000):
        key = f"agent:main:telegram:dm:{index}"
        entries[key] = json.dumps({
            "session_id": f"session-{index}", "session_key": key, "created_at": "2026-11-01T12:00:00+00:00",
            "updated_at": "2026-11-20T12:00:00+00:00", "origin": {"platform": "telegram", "chat_id": str(index)},
            "prompt_pin": {"version": 1, "context_prompt": "pinned context " * 64},
        })
    holder._write_sql("INSERT INTO gateway_routing VALUES ('bench', ?, ?, 1)", entries.items(), many=True)
    holder._write_sql("INSERT INTO state_meta VALUES (?, '{}')", [(RETIRED_PREFIX + "unrelated",),
                      (route_fence_key("main", "another"),)], many=True)
    execute_write = holder._execute_write
    samples, holds, route_writes, end_to_end = [], [], [], []
    can_commit_fence = threading.Event()
    mutex = _holder.writer_mutex_until

    @contextmanager
    def measure_mutex(*args, **kwargs):
        with mutex(*args, **kwargs):
            acquired = time.perf_counter()
            can_commit_fence.set()
            yield
        holds.append((time.perf_counter() - acquired) * 1000)

    for event, row in [('insert', 'new'), ('update', 'new'), ('delete', 'old')]:
        holder._conn.execute(f'CREATE TEMP TRIGGER count_route_{event} AFTER {event} ON gateway_routing '
                             f'BEGIN SELECT record_write({row}.session_key); END')
    holder._conn.create_function('record_write', 1, route_writes.append)
    monkeypatch.setattr(_holder, 'writer_mutex_until', measure_mutex)

    def measured(callback):
        statements = []
        vm_blocks = []
        before = holder._conn.total_changes
        holder._conn.set_progress_handler(lambda: vm_blocks.append(1), 64)
        holder._conn.set_trace_callback(lambda sql: statements.append(sql.partition(' ')[0]))
        start = time.perf_counter()
        try:
            return execute_write(callback)
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            holder._conn.set_progress_handler(None, 0)
            holder._conn.set_trace_callback(None)
            samples.append((elapsed, holder._conn.total_changes - before, len(statements), len(vm_blocks)))

    monkeypatch.setattr(holder, "_execute_write", measured)
    key = "agent:main:telegram:dm:5000"
    with closing(sqlite3.connect(holder.db_path)) as reader:
        reader.execute('BEGIN')
        reader.execute('SELECT entry_json FROM gateway_routing LIMIT 1').fetchone()
        assert not store.routing.replace_routing_entries(entries, routing_scope="bench")
        assert samples[-1][1] == 0
        for revision in range(12):
            entries[key] = json.dumps({**json.loads(entries[key]), "revision": revision})
            start = time.perf_counter()
            assert not store.routing.replace_routing_entries(entries, routing_scope="bench")
            end_to_end.append((time.perf_counter() - start) * 1000)
    assert len(samples) == 13
    assert route_writes == [key] * 12
    elapsed = [sample[0] for sample in samples[1:]]
    print(f"10k hot writer, acquired to released: median={statistics.median(holds[1:]):.3f} ms; "
          f"max={max(holds[1:]):.3f} ms; inclusive of wait: median={statistics.median(elapsed):.3f} ms; "
          f"max={max(elapsed):.3f} ms; full save median/max={statistics.median(end_to_end):.2f}/{max(end_to_end):.2f} ms; "
          f"mutex ms={holds}; samples (ms, writes, statements, 64-VM blocks)={samples}")
    assert all(writes <= 3 and statements < 30 and blocks < 16 for _, writes, statements, blocks in samples), samples
    assert holder._read_one("SELECT count(*) FROM gateway_routing WHERE updated_at = 1")[0] == 9999
    text, stamp = holder._read_one("SELECT entry_json, updated_at FROM gateway_routing WHERE session_key = ?", (key,))
    assert text == entries[key] and stamp > 1

    staged, locked = threading.Event(), threading.Event()
    can_commit_fence.clear()

    def fence():
        assert staged.wait(10)
        with closing(sqlite3.connect(holder.db_path)) as other, other:
            other.execute("INSERT INTO state_meta VALUES (?, '{}')", (route_fence_key('main', 'competitor'),))
            locked.set()
            assert can_commit_fence.wait(10)

    def contended(callback):
        staged.set()
        assert locked.wait(10)
        return measured(callback)

    monkeypatch.setattr(holder, '_execute_write', contended)
    with closing(sqlite3.connect(holder.db_path)) as reader, ThreadPoolExecutor(max_workers=1) as pool:
        reader.execute('BEGIN')
        reader.execute('SELECT entry_json FROM gateway_routing LIMIT 1').fetchone()
        writer = pool.submit(fence)
        entries[key] = json.dumps({**json.loads(text), 'session_id': 'competitor'})
        assert store.routing.replace_routing_entries(entries, routing_scope='bench') == {key}
        writer.result(10)
    assert len(samples) == 15 and [sample[1] for sample in samples[-2:]] == [0, 2]
    assert all(count < 30 and blocks < 16 for _, _, count, blocks in samples[-2:])
    print(f'10k competing reader/fencer: stale then commit holds ms={holds[-2:]}; samples={samples[-2:]}')

    entries[key] = json.dumps({**json.loads(text), "revision": "repair"})
    monkeypatch.setattr(holder, '_execute_write', execute_write)
    before = holder._conn.total_changes
    assert not store.routing.replace_routing_entries(entries, routing_scope="bench", rewrite=True)
    assert holder._conn.total_changes - before >= 2 * len(entries) - 1
    assert not holder._read_one('SELECT 1 FROM gateway_routing WHERE updated_at = 1')


@pytest.mark.parametrize("key", ["", "agent", "agent:", "agent::x", "agent:k", "agent:k:", "agent:k:x:y",
                                 "x:k:y", "Agent:k:x", "agent:🌖λ:δ", "agent:a\0b:rest", "全:界:界"])
def test_route_namespace_sql_matches_python(backend, scope_a, key):
    sql = route_namespace_sql(":key")
    assert backend.open(scope_a).holder()._read_one(f"SELECT {sql}", {"key": key})[0] == route_namespace(key)


def _seed_case(store, routes, markers, live):
    def seed(conn):
        conn.execute("DELETE FROM gateway_routing")
        conn.execute("DELETE FROM state_meta")
        conn.execute("DELETE FROM sessions")
        conn.executemany("INSERT INTO gateway_routing VALUES (?, ?, ?, 1)", routes)
        conn.executemany("INSERT INTO state_meta VALUES (?, ?)", markers.items())
        conn.executemany("INSERT INTO sessions (id, source, started_at) VALUES (?, 'telegram', 1)", live)
    store.holder()._execute_write(seed)


def _rows(store):
    return [tuple(row) for row in store.holder()._read_all(
        "SELECT scope, session_key, entry_json FROM gateway_routing ORDER BY scope, session_key")]


def test_diff_matches_rewrite_across_retirements_and_scopes(backend, scope_a, scope_b):
    diff, repair = backend.open(scope_a), backend.open(scope_b)
    rng = random.Random(85001)
    keys = ("agent:main:one", "agent:kepler:two", "agent::x", "agent:k", "foo:bar:baz", "agent:δ:", "plain")
    targets = ("local", "peer", "s:colon:δ", "null\0byte", 12, 1, 1.0, None)
    payloads = [json.dumps({"session_id": sid}) for sid in targets] + ["{}", "[]", "null"]
    now = time.time()
    values = ["{}", '{"own":true}', '{"own":false}', "not json", '{"expires":true,"retired_at":"0"}',
              '{"expires":"0","retired_at":0}', *(
        json.dumps({"expires": True, "retired_at": now - shift}) for shift in (0, 2 * RETRY_HORIZON_S))]
    for case in range(200):
        scope = rng.choice(("one", "two"))
        initial = {name: {key: rng.choice(payloads) for key in keys if rng.randrange(4)} for name in ("one", "two")}
        forced = {"unchanged-marked": '{"session_id":"marked"}', "agent:kepler:fenced": '{"session_id":"fenced"}'}
        initial[scope].update(forced)
        routes = [(name, key, text) for name, contents in initial.items() for key, text in contents.items()]
        entries = {key: rng.choice((initial[scope].get(key, ""), rng.choice(payloads))) for key in keys if rng.randrange(4)}
        entries.update({"": "{}", "empty": "", **forced})
        markers = {RETIRED_PREFIX + "marked": '{}', route_fence_key("kepler", "fenced"): '{}'}
        for sid in targets:
            for marker in (RETIRED_PREFIX + str(sid), *(route_fence_key(ns, sid) for ns in ("main", "kepler", "δ"))):
                if not rng.randrange(3):
                    markers[marker] = rng.choice(values)
        live = [(str(sid),) for sid in targets if rng.randrange(2)]
        for store in (diff, repair):
            _seed_case(store, routes, markers, live)
        expected = repair.routing.replace_routing_entries(entries, routing_scope=scope, rewrite=True)
        assert forced.keys() <= expected, case
        assert diff.routing.replace_routing_entries(entries, routing_scope=scope) == expected, case
        assert _rows(diff) == _rows(repair), case


@pytest.mark.parametrize("rewrite", [False, True])
def test_retained_invalid_json_rolls_back_but_removed_invalid_json_does_not(backend, scope_a, rewrite):
    store = backend.open(scope_a)
    _seed_case(store, [("one", "bad", "{"), ("one", "obsolete", "{}")], {}, [])
    old = _rows(store)
    with pytest.raises(PermanentStoreError, match="malformed JSON"):
        store.routing.replace_routing_entries({"bad": "{"}, routing_scope="one", rewrite=rewrite)
    assert _rows(store) == old
    assert not store.routing.replace_routing_entries({"new": "{}"}, routing_scope="one", rewrite=rewrite)
    assert _rows(store) == [("one", "new", "{}")]


def test_external_writers_between_snapshot_and_commit(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    holder = store.holder()
    desired = {key: json.dumps({'session_id': key}) for key in ('deleted', 'repointed', 'moved', 'retired')}
    desired['incoming'] = '{}'
    _seed_case(store, [('one', key, text) for key, text in desired.items() if key != 'incoming'], {}, [])
    execute = holder._execute_write
    attempts = []

    def race(callback):
        attempts.append(None)
        with closing(sqlite3.connect(holder.db_path)) as other, other:
            if len(attempts) == 1:
                other.execute("DELETE FROM gateway_routing WHERE session_key='deleted'")
                other.execute("UPDATE gateway_routing SET entry_json='{}' WHERE session_key='repointed'")
                other.execute("UPDATE gateway_routing SET scope='two' WHERE session_key='moved'")
                other.execute("INSERT INTO gateway_routing VALUES ('one','extra','{}',1)")
                other.execute("INSERT INTO state_meta VALUES (?, '{}')", (route_fence_key('main', 'retired'),))
            other.execute("INSERT INTO sessions (id,source,started_at) VALUES (?, 'telegram', 1)", (str(len(attempts)),))
            other.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES (?, 'user','hi',2)",
                          (str(len(attempts)),))
            other.execute("INSERT OR REPLACE INTO gateway_routing VALUES ('unrelated','hot','{}',?)", (len(attempts),))
        assert len(attempts) <= 2, 'unrelated activity must not invalidate a routing snapshot'
        return execute(callback)

    monkeypatch.setattr(holder, '_execute_write', race)
    assert store.routing.replace_routing_entries(desired, routing_scope='one') == {'retired'}
    assert dict(store.routing.load_routing_entries(routing_scope='one')) == {
        key: text for key, text in desired.items() if key != 'retired'}
    assert dict(store.routing.load_routing_entries(routing_scope='two')) == {'moved': desired['moved']}


@pytest.mark.parametrize('start_owned', [False, True])
def test_main_fence_ownership_race(backend, scope_a, monkeypatch, start_owned):
    store = backend.open(scope_a)
    text = '{"session_id":"target"}'
    _seed_case(store, [('one', 'main-key', text)], {route_fence_key('main', 'target'): '{}'},
               [('target',)] if start_owned else [])
    holder = store.holder()
    execute = holder._execute_write
    raced = False

    def race(callback):
        nonlocal raced
        if not raced:
            with closing(sqlite3.connect(holder.db_path)) as other, other:
                if start_owned:
                    other.execute("DELETE FROM sessions WHERE id='target'")
                else:
                    other.execute("INSERT INTO sessions(id,source,started_at) VALUES ('target','telegram',1)")
            raced = True
        return execute(callback)

    monkeypatch.setattr(holder, '_execute_write', race)
    assert store.routing.replace_routing_entries({'main-key': text}, routing_scope='one') == (
        {'main-key'} if start_owned else set())
    assert bool(_rows(store)) is not start_owned


def test_delta_failure_rolls_back_obsolete_delete(backend, scope_a):
    store = backend.open(scope_a)
    _seed_case(store, [('one', 'keep', '{}'), ('one', 'obsolete', '{}')], {}, [])
    store.holder()._write_sql("CREATE TRIGGER refuse BEFORE INSERT ON gateway_routing WHEN NEW.session_key='fail' "
                              "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(PermanentStoreError, match='injected failure'):
        store.routing.replace_routing_entries({'keep': '{}', 'fail': '{}'}, routing_scope='one')
    assert _rows(store) == [('one', 'keep', '{}'), ('one', 'obsolete', '{}')]


def test_expiry_crossing_commit_replans_without_parsing_json_on_writer(backend, scope_a, monkeypatch):
    from persistence.sqlite.gateway import _delta

    store = backend.open(scope_a)
    holder = store.holder()
    key, text, stamp = 'agent:k:peer', '{"session_id":"retired"}', time.time()
    _seed_case(store, [], {route_fence_key('k', 'retired'): json.dumps({'expires': True, 'retired_at': stamp})}, [])
    monkeypatch.setattr(holder, '_checkout_read_conn', lambda: None)
    execute = holder._execute_write
    clock = [stamp + RETRY_HORIZON_S]
    monkeypatch.setattr(_delta.time, 'time', lambda: clock[0])

    def race(callback):
        clock[0] += 1
        return execute(callback)

    def no_writer_json(action, first, second, db, trigger):
        if action == sqlite3.SQLITE_FUNCTION and second.startswith('json_'):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    monkeypatch.setattr(holder, '_execute_write', race)
    holder._conn.set_authorizer(no_writer_json)
    try:
        assert not store.routing.replace_routing_entries({key: text}, routing_scope='one')
        assert _rows(store) == [('one', key, text)]
    finally:
        holder._conn.set_authorizer(None)


def test_direct_sql_replace_displacing_a_marker_even_with_recursive_triggers_off(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    holder = store.holder()
    text = '{"session_id":"target"}'
    _seed_case(store, [], {RETIRED_PREFIX + 'target': '{}'}, [])
    execute = holder._execute_write

    def race(callback):
        with closing(sqlite3.connect(holder.db_path)) as other, other:
            other.execute('PRAGMA recursive_triggers=OFF')
            other.execute("INSERT OR REPLACE INTO state_meta(rowid,key,value) "
                          "SELECT rowid, 'unrelated', '{}' FROM state_meta WHERE key=?", (RETIRED_PREFIX + 'target',))
        return execute(callback)

    monkeypatch.setattr(holder, '_execute_write', race)
    assert not store.routing.replace_routing_entries({'key': text}, routing_scope='one')
    assert _rows(store) == [('one', 'key', text)]


def test_rowid_replace_moving_a_route_out_of_scope_invalidates_old_scope(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    _seed_case(store, [('one', 'key', '{}')], {}, [])
    holder = store.holder()
    execute = holder._execute_write

    def race(callback):
        with closing(sqlite3.connect(holder.db_path)) as other, other:
            other.execute("INSERT OR REPLACE INTO gateway_routing(rowid,scope,session_key,entry_json,updated_at) "
                          "SELECT rowid, 'other', 'new', '{}', 2 FROM gateway_routing WHERE scope='one'")
        return execute(callback)

    monkeypatch.setattr(holder, '_execute_write', race)
    assert not store.routing.replace_routing_entries({'key': '{}'}, routing_scope='one')
    assert _rows(store) == [('one', 'key', '{}'), ('other', 'new', '{}')]


def test_replace_inside_stale_transcript_snapshot_validates_fresh_routes(backend, scope_a):
    store = backend.open(scope_a)
    holder = store.holder()
    _seed_case(store, [('one', 'keep', '{}')], {}, [])
    with holder.transcript_read_snapshot():
        assert _rows(store) == [('one', 'keep', '{}')]
        with closing(sqlite3.connect(holder.db_path)) as other, other:
            other.execute("INSERT INTO gateway_routing VALUES ('one', 'obsolete', '{}', 1)")
        assert not store.routing.replace_routing_entries({'keep': '{}'}, routing_scope='one')
    assert _rows(store) == [('one', 'keep', '{}')]
