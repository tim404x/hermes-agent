"""Write cost and retirement semantics of replacing one routing scope."""

import json
import random
import time

import pytest

from persistence.authority.vocabulary import RETRY_HORIZON_S
from persistence.core.errors import PermanentStoreError
from persistence.gateway.routing import route_namespace
from persistence.sqlite.conversation._store.retirement import RETIRED_PREFIX
from persistence.sqlite.gateway._routes import route_fence_key, route_namespace_sql


def test_one_changed_route_among_10k_writes_o1_rows_and_holds_writer_briefly(backend, scope_a, monkeypatch):
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
    samples = []

    def measured(callback):
        before = holder._conn.total_changes
        start = time.perf_counter()
        result = execute_write(callback)
        samples.append((time.perf_counter() - start, holder._conn.total_changes - before))
        return result

    monkeypatch.setattr(holder, "_execute_write", measured)
    key = "agent:main:telegram:dm:5000"
    for revision in range(3):
        entries[key] = json.dumps({**json.loads(entries[key]), "revision": revision})
        assert not store.routing.replace_routing_entries(entries, routing_scope="bench")
        assert len(samples) == revision + 1
        elapsed, writes = samples[-1]
        assert writes <= 3, (elapsed, writes)
        if elapsed < 0.100:
            break
    assert elapsed < 0.100, samples
    assert holder._read_one("SELECT count(*) FROM gateway_routing WHERE updated_at = 1")[0] == 9999
    text, stamp = holder._read_one("SELECT entry_json, updated_at FROM gateway_routing WHERE session_key = ?", (key,))
    assert text == entries[key] and stamp > 1

    entries[key] = json.dumps({**json.loads(entries[key]), "revision": "repair"})
    assert not store.routing.replace_routing_entries(entries, routing_scope="bench", rewrite=True)
    repair_elapsed, repair_writes = samples[-1]
    assert repair_writes == 2 * len(entries)
    print(f"10k routes: diff {elapsed * 1000:.1f} ms / {writes} writes; "
          f"rewrite {repair_elapsed * 1000:.1f} ms / {repair_writes} writes")


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
    targets = ("local", "peer", "s:colon:δ", "null\0byte", 12, None)
    payloads = [json.dumps({"session_id": sid}) for sid in targets] + ["{}", "[]", "null"]
    now = time.time()
    values = ["{}", '{"own":true}', '{"own":false}', "not json", *(
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
