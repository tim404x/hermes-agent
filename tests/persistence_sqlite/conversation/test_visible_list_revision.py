"""The visible list revision (``list_revision``) moves on every write a human-facing listing can show and holds
through worker-run writes, and the listing memo keys such listings on it; the local-lineage receipt pass is an
index seek (#152508). Fork carry, desktop speed pass (add45)."""

import json
import os
import time
import sqlite3
from contextlib import closing

import pytest

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import (INTERNAL_LISTING_SOURCES, NewSession, SessionCountQuery, SessionQuery,
                                               SessionWindow)
from persistence.sqlite.conversation import sessions as sessions_module
from persistence.sqlite.conversation._store import list_revision, listing_memo, local, local_lineage_memo
from persistence.sqlite.conversation._store.local import LOCAL_LINEAGE_LEN_EXPR, POLICY_PREFIX, local_lineage_index
from persistence_assembly import open_persistence_reader
from tests.persistence_sqlite.conftest import store_for

VISIBLE = SessionQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron"))
TRIGGER_SQL_DIGEST_V1 = "e944a2fccd5f4fb5"


@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.delenv("HERMES_LISTING_MEMO", raising=False)
    # Past the racy window of every file these tests just wrote (test_listing_memo's QUIET clock).
    monkeypatch.setattr(listing_memo, "_now_ns", lambda: time.time_ns() + 10_000_000_000)
    listing_memo.clear()
    yield
    listing_memo.clear()


@pytest.fixture
def store(backend, scope_a):
    store = backend.open(scope_a)
    store.sessions.create(NewSession("chat", "desktop"))
    store.sessions.create(NewSession("job", "oneshot"))
    return store


def _db(scope):
    return closing(sqlite3.connect(store_for(scope)))


def _revision(scope):
    with _db(scope) as conn:
        return list_revision.visible_list_revision(conn)


def _moves(scope, write) -> bool:
    before = _revision(scope)
    write()
    after = _revision(scope)
    assert before is not None and after is not None
    return after != before


def _sql(scope, statement, params=()):
    def run():
        with _db(scope) as conn:
            conn.execute(statement, params)
            conn.commit()
    return run


def test_a_current_store_has_the_triggers_and_the_receipt_index(store, scope_a):
    with _db(scope_a) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger', 'index')")}
    assert set(list_revision.TRIGGER_NAMES) <= names
    assert local.LOCAL_LINEAGE_INDEX in names
    assert _revision(scope_a) is not None


def test_internal_sources_are_the_listing_deny_list_plus_cron():
    assert tuple(list_revision.INTERNAL_SOURCES) == (*INTERNAL_LISTING_SOURCES, "cron")


def test_cron_runs_hold_the_revision_unless_they_shape_a_visible_row(store, scope_a):
    assert not _moves(scope_a, lambda: store.sessions.create(NewSession("nightly", "cron")))
    assert not _moves(scope_a, lambda: store.messages.append("nightly", NewMessage(role="assistant", content="done")))
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET last_activity_at = 9 WHERE id = 'nightly'"))
    # A cron run continued on a human surface: the continuation and then every write of the run move it.
    assert _moves(scope_a, lambda: store.sessions.create(NewSession("follow-up", "desktop", parent_session_id="nightly")))
    assert _moves(scope_a, lambda: store.messages.append("nightly", NewMessage(role="assistant", content="again")))


def test_the_trigger_sql_is_pinned_to_its_version():
    # Readers trust the trigger set by its (versioned) names: a changed body under the same names would make other
    # builds key listings on a revision that means something else. Bump TRIGGER_VERSION, then this digest.
    import hashlib

    digest = hashlib.sha256(list_revision.VISIBLE_LIST_REVISION_SQL.encode()).hexdigest()[:16]
    assert (list_revision.TRIGGER_VERSION, digest) == (1, TRIGGER_SQL_DIGEST_V1)
    assert all(f"_v{list_revision.TRIGGER_VERSION}_" in name for name in list_revision.TRIGGER_NAMES)


def test_an_index_of_that_name_on_another_expression_keeps_the_memoized_pass(store, scope_a):
    with _db(scope_a) as conn:
        assert local._receipts_indexed(conn)
        conn.execute("DROP INDEX idx_state_meta_local_lineage")
        conn.execute("CREATE INDEX idx_state_meta_local_lineage ON state_meta(key)")
        conn.commit()
        assert not local._receipts_indexed(conn)


def test_a_writable_reopen_leaves_the_settled_triggers_alone(store, scope_a, backend):
    # A DROP/CREATE on every open would take the write lock each time and move the schema cookie (every kept
    # answer would miss after any process opened the store).
    with _db(scope_a) as conn:
        cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
    from persistence.sqlite._holder import StoreHolder

    StoreHolder(store_for(scope_a)).close()
    with _db(scope_a) as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == cookie


def test_worker_run_writes_hold_the_revision(store, scope_a):
    assert not _moves(scope_a, lambda: store.messages.append("job", NewMessage(role="user", content="tick")))
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET tool_call_count = tool_call_count + 1, "
                                             "last_activity_at = 1 WHERE id = 'job'"))
    assert not _moves(scope_a, lambda: store.sessions.create(NewSession("job2", "kanban")))
    # A compression continuation of a worker run is a worker run too.
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'compression' WHERE id = 'job'"))
    assert not _moves(scope_a, lambda: store.sessions.create(NewSession("job-tip", "oneshot", parent_session_id="job")))
    assert not _moves(scope_a, lambda: store.messages.append("job-tip", NewMessage(role="assistant", content="ok")))
    # A sub-agent run (delegate marker) of a visible chat: no listing that hides sub-agents admits or walks it.
    assert not _moves(scope_a, _sql(scope_a, "INSERT INTO sessions(id, source, parent_session_id, model_config, "
                                             "started_at) VALUES ('sub', 'subagent', 'chat', ?, 1)",
                                    (json.dumps({"_delegate_from": "chat"}),)))
    assert not _moves(scope_a, lambda: store.messages.append("sub", NewMessage(role="user", content="go")))


def test_visible_writes_move_the_revision(store, scope_a):
    assert _moves(scope_a, lambda: store.messages.append("chat", NewMessage(role="user", content="hello")))
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET title = 'renamed' WHERE id = 'chat'"))
    assert _moves(scope_a, lambda: store.sessions.create(NewSession("other", "telegram")))
    assert _moves(scope_a, _sql(scope_a, "UPDATE messages SET content = 'edited' WHERE session_id = 'chat'"))
    assert _moves(scope_a, _sql(scope_a, "DELETE FROM messages WHERE session_id = 'chat'"))
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET source = 'desktop' WHERE id = 'job'"))  # retagged


def test_rows_that_shape_a_visible_listing_move_it_whatever_their_source(store, scope_a):
    # A worker-sourced compression continuation of a VISIBLE chat is that chat's tip.
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'compression' WHERE id = 'chat'"))
    assert _moves(scope_a, lambda: store.sessions.create(NewSession("chat-tip", "oneshot", parent_session_id="chat")))
    assert _moves(scope_a, lambda: store.messages.append("chat-tip", NewMessage(role="user", content="resumed")))
    assert _moves(scope_a, lambda: store.sessions.create(NewSession("chat-tip2", "oneshot", parent_session_id="chat-tip")))
    # A worker run with a visible (branch/reset) child: the child's admission reads its parent.
    assert _moves(scope_a, lambda: store.sessions.create(NewSession("branch", "desktop", parent_session_id="job")))
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET ended_at = 5 WHERE id = 'job'"))
    # A dangling parent id (a store whose parent row was pruned) counts as shown.
    assert _moves(scope_a, _sql(scope_a, "INSERT INTO sessions(id, source, parent_session_id, started_at) "
                                         "VALUES ('lost', 'oneshot', 'nowhere', 1)"))


def test_multi_segment_receipts_move_it(store, scope_a):
    receipt = json.dumps({"session_id": "job", "lineage": ["job", "job-b"]})
    assert _moves(scope_a, _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, ?)", (POLICY_PREFIX + "job", receipt)))
    # The lineage's segments now shape the listing: the worker run's own writes move it.
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'session_reset' WHERE id = 'job'"))
    single = json.dumps({"session_id": "x", "lineage": ["x"]})
    assert not _moves(scope_a, _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, ?)", (POLICY_PREFIX + "x", single)))
    assert not _moves(scope_a, _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES ('unrelated', '1')"))


def test_a_compressed_worker_run_lineage_holds_the_revision(store, scope_a):
    # A compressed one-shot run: its local receipt names only worker segments, which no visible listing shows.
    store.sessions.create(NewSession("job-b", "oneshot", parent_session_id="job"))
    # Every segment of a local lineage carries the conversation's chat id, the receipt's owner.
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET chat_id = 'job' WHERE id IN ('job', 'job-b')"))
    receipt = json.dumps({"session_id": "job", "lineage": ["job", "job-b"]})
    assert not _moves(scope_a, _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, ?)", (POLICY_PREFIX + "job", receipt)))
    assert not _moves(scope_a, lambda: store.messages.append("job-b", NewMessage(role="user", content="tick")))
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'session_reset' WHERE id = 'job'"))
    # A segment the receipt names but the store does not hold (yet) counts as shown.
    grown = json.dumps({"session_id": "job", "lineage": ["job", "job-b", "job-c"]})
    assert _moves(scope_a, _sql(scope_a, "UPDATE state_meta SET value = ? WHERE key = ?", (grown, POLICY_PREFIX + "job")))
    # A shown lineage reads its segments' ids and end_reason only: messages and heartbeats hold, a reset moves it.
    assert not _moves(scope_a, lambda: store.messages.append("job-b", NewMessage(role="user", content="tick")))
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET last_activity_at = 7 WHERE id = 'job-b'"))
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'session_reset' WHERE id = 'job-b'"))
    store.sessions.create(NewSession("job-c", "oneshot", parent_session_id="job-b"))  # a worker row: holds
    assert not _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = NULL WHERE id = 'job-b'"))
    # A segment on a human surface shapes the listing: the receipt write and the segments' resets move it.
    store.sessions.create(NewSession("desk-seg", "desktop"))
    mixed = json.dumps({"session_id": "job", "lineage": ["job", "job-b", "desk-seg"]})
    assert _moves(scope_a, _sql(scope_a, "UPDATE state_meta SET value = ? WHERE key = ?", (mixed, POLICY_PREFIX + "job")))
    assert not _moves(scope_a, lambda: store.messages.append("job-b", NewMessage(role="user", content="tick")))
    assert _moves(scope_a, _sql(scope_a, "UPDATE sessions SET end_reason = 'session_reset' WHERE id = 'job-b'"))
    assert _moves(scope_a, _sql(scope_a, "DELETE FROM state_meta WHERE key = ?", (POLICY_PREFIX + "job",)))
    # Bad JSON in a receipt never fails the write, and names nothing.
    assert not _moves(scope_a, _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, '{bad')", (POLICY_PREFIX + "job",)))


def test_a_missing_trigger_or_a_rebuilt_schema_never_vouches(store, scope_a):
    _sql(scope_a, f"DROP TRIGGER {list_revision.TRIGGER_NAMES[4]}")()
    assert _revision(scope_a) is None
    store.sessions.create(NewSession("again", "desktop"))  # no reopen: still missing
    assert _revision(scope_a) is None


def _browse(scope, fn):
    with open_persistence_reader(store_for(scope).parent) as reader:
        return fn(reader)


def _count_calls(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def _visible_ids(reader):
    return [str(e.session.session_id) for e in reader.sessions.list_sessions(VISIBLE, SessionWindow(limit=20), compact_rows=True)]


def test_a_visible_listing_is_kept_through_worker_writes_and_redone_after_a_visible_one(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    assert _browse(scope_a, _visible_ids) == ["chat"]
    for i in range(3):  # the store files move on every one of these: the stat signature would miss each time
        store.messages.append("job", NewMessage(role="user", content=f"tick {i}"))
        assert _browse(scope_a, _visible_ids) == ["chat"]
    assert len(reads) == 1
    store.sessions.create(NewSession("new-chat", "desktop"))
    assert sorted(_browse(scope_a, _visible_ids)) == ["chat", "new-chat"]
    assert len(reads) == 2


def test_a_store_latched_corrupt_is_never_answered_from_memory(store, scope_a, monkeypatch):
    # A damaged page runs no trigger: once any read latched the store corrupt, every listing reads (and fails) again.
    from persistence.sqlite._state.health import mark_storage_corrupt, reset_storage_state

    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    assert _browse(scope_a, _visible_ids) == ["chat"]
    assert _browse(scope_a, _visible_ids) == ["chat"]
    assert len(reads) == 1
    mark_storage_corrupt(store_for(scope_a), "test")
    try:
        _browse(scope_a, _visible_ids)
        _browse(scope_a, _visible_ids)
        assert len(reads) == 3
    finally:
        reset_storage_state()


def test_counts_of_visible_rows_follow_the_revision(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "session_count")
    query = SessionCountQuery(exclude_children=True, exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron"))
    assert _browse(scope_a, lambda r: r.sessions.count_sessions(query)) == 1
    store.messages.append("job", NewMessage(role="user", content="tick"))
    assert _browse(scope_a, lambda r: r.sessions.count_sessions(query)) == 1
    assert len(reads) == 1
    store.sessions.create(NewSession("new-chat", "cli"))
    assert _browse(scope_a, lambda r: r.sessions.count_sessions(query)) == 2


def test_a_listing_that_can_show_worker_rows_keeps_the_stat_signature(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    everything = lambda r: [str(e.session.session_id) for e in r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=20), compact_rows=True)]
    assert sorted(_browse(scope_a, everything)) == ["chat", "job"]
    store.sessions.create(NewSession("job2", "oneshot"))
    assert sorted(_browse(scope_a, everything)) == ["chat", "job", "job2"]
    assert len(reads) == 2


@pytest.mark.parametrize("query, visible", [
    (SessionQuery(), False),
    (SessionQuery(exclude_sources=INTERNAL_LISTING_SOURCES), False),  # shows cron runs
    (SessionQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron")), True),
    (SessionQuery(exclude_sources=("kanban", "oneshot", "cron")), False),
    (SessionQuery(sources=("desktop", "telegram")), True),
    (SessionQuery(sources=("cron",)), False),
    (SessionQuery(sources=("desktop", "oneshot")), False),
    (SessionQuery(sources=()), False),  # an empty allow-list filters nothing
    (SessionQuery(sources=(), exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron")), True),
    (SessionQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron"), include_children=True), False),
    (SessionQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron"), include_subagents=True), False),
    (SessionCountQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron")), False),  # counts children unless told not to
    (SessionCountQuery(exclude_sources=(*INTERNAL_LISTING_SOURCES, "cron"), exclude_children=True), True),
])
def test_which_queries_may_key_on_the_visible_revision(query, visible):
    assert sessions_module._visible_listing(query) is visible


def test_the_memo_falls_back_to_the_stat_signature_without_triggers(store, scope_a, monkeypatch):
    _sql(scope_a, f"DROP TRIGGER {list_revision.TRIGGER_NAMES[0]}")()
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    assert _browse(scope_a, _visible_ids) == ["chat"]
    store.messages.append("job", NewMessage(role="user", content="tick"))
    assert _browse(scope_a, _visible_ids) == ["chat"]
    assert len(reads) == 2  # the files moved and nothing vouches for the listing: read again


def test_revision_and_stat_answers_do_not_evict_each_other(store, scope_a):
    _browse(scope_a, _visible_ids)
    _browse(scope_a, lambda r: r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=5), compact_rows=True))
    kinds = {listing_memo._family(kept[0]) for kept in listing_memo._memo.values()}
    assert "rev" in kinds
    hits = listing_memo.stats()
    assert any(k[1] == "rev" for k in hits)


# ── local lineage receipts: one index seek (#152508 port) ──

def _receipts(conn):
    receipts = {
        "two": {"session_id": "two", "lineage": ["two", "three"]},
        "three": {"session_id": "three", "lineage": ["three", "four", "five"]},
        "one": {"session_id": "one", "lineage": ["one"]},
        "empty": {"session_id": "empty"},
        "number-owner": {"session_id": 7, "lineage": ["a", "b"]},
        "object-owner": {"session_id": {"x": 1}, "lineage": ["c", "d"]},
        "non-text-segment": {"session_id": "n", "lineage": ["n", 3]},
    }
    conn.executemany("INSERT INTO state_meta(key, value) VALUES (?, ?)",
                     ((POLICY_PREFIX + name, json.dumps(body)) for name, body in receipts.items()))
    conn.execute("INSERT INTO state_meta(key, value) VALUES (?, ?)", (POLICY_PREFIX + "broken", "{not json"))
    conn.execute("INSERT INTO state_meta(key, value) VALUES (?, ?)",
                 ("gateway.api.binding.v1:x", json.dumps({"session_id": "x", "lineage": ["a", "b"]})))
    conn.executemany("INSERT INTO state_meta(key, value) VALUES (?, ?)", (
        (POLICY_PREFIX + f"plain-{i}", json.dumps({"session_id": f"plain-{i}", "lineage": [f"plain-{i}"]}))
        for i in range(200)))
    conn.commit()


def _old_index(conn):
    """The receipt scan as it was before the index (Python parse of every selected receipt)."""
    superseded, heads = set(), {}
    rows = conn.execute("SELECT value FROM state_meta NOT INDEXED WHERE key GLOB ? AND json_valid(value) "
                        "AND json_array_length(value, '$.lineage') > 1", (POLICY_PREFIX + "*",)).fetchall()
    for (raw,) in rows:
        receipt = json.loads(raw)
        owner, lineage = receipt.get("session_id"), receipt.get("lineage")
        if not isinstance(owner, str) or not all(isinstance(sid, str) for sid in lineage):
            continue
        marks = ",".join("?" * len(lineage))
        ended = dict(conn.execute(f"SELECT id,end_reason FROM sessions WHERE id IN ({marks})", lineage).fetchall())
        starts = [sid for i, sid in enumerate(lineage) if i == 0 or ended.get(lineage[i - 1]) == "session_reset"]
        superseded.update(starts[:-1])
        heads[starts[-1]] = (owner, lineage)
    return superseded, heads


def test_the_receipt_pass_seeks_the_index_and_answers_as_the_scan_did(store, scope_a):
    with _db(scope_a) as conn:
        _receipts(conn)
        conn.execute("INSERT INTO sessions(id, source, started_at, end_reason) VALUES ('three', 'local', 1, 'session_reset')")
        conn.commit()
        plan = " | ".join(row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {local._MULTI_SEGMENT_RECEIPTS_SQL}"))
        assert "USING INDEX idx_state_meta_local_lineage" in plan, plan
        assert "SCAN" not in plan, plan
        assert local_lineage_index(conn) == _old_index(conn)
        superseded, heads = local_lineage_index(conn)
        assert superseded == {"three"} and set(heads) == {"two", "four"}


def test_an_indexed_store_skips_the_python_receipt_memo(store, scope_a, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the receipt memo walked an indexed store")

    monkeypatch.setattr(local_lineage_memo, "memoized_local_lineage_index", refuse)
    with _db(scope_a) as conn:
        _receipts(conn)
    assert _browse(scope_a, lambda r: sorted(str(e.session.session_id) for e in r.sessions.list_sessions(
        SessionQuery(), SessionWindow(limit=20)))) == ["chat", "job"]


def test_a_store_without_the_index_keeps_the_memoized_receipt_pass(store, scope_a, monkeypatch):
    _sql(scope_a, f"DROP INDEX {local.LOCAL_LINEAGE_INDEX}")()
    calls = _count_calls(monkeypatch, local_lineage_memo, "memoized_local_lineage_index")
    _browse(scope_a, lambda r: r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=20), compact_rows=True))
    assert calls


# --- Old SQLite builds must still read (and create) the schema (integrated review #2, finding 1) -------------------
# SQLite < 3.46 parses with a fixed 100-entry LALR stack. A trigger whose ancestor walk nested one subquery per hop
# overflowed it from 4 hops on (paren depth 18; 15 still parsed on 3.45.1), and SQLite parses the WHOLE schema on
# first use, so one such trigger made the store unreadable to every older build: this VPS's system sqlite3 CLI and
# /usr/bin/python3 (3.45.1), macOS's sqlite3, the recovery salvage CLI (find_sqlite3_cli).
_PAREN_DEPTH_BOUND = 12
_OLD_SQLITE = (3, 46, 0)


def _paren_depth(sql: str) -> int:
    depth = deepest = 0
    quoted = False
    for ch in sql:
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif not quoted and ch == ")":
            depth -= 1
    return deepest


def test_the_generated_ddl_nesting_is_bounded_and_does_not_grow_with_the_hop_count(monkeypatch):
    # Portable guard: runs where no old SQLite is installed (CI).
    from persistence.sqlite._schema_ddl import DEFERRED_INDEX_SQL

    assert _paren_depth(list_revision.VISIBLE_LIST_REVISION_SQL) <= _PAREN_DEPTH_BOUND
    assert _paren_depth(DEFERRED_INDEX_SQL) <= _PAREN_DEPTH_BOUND
    depths = set()
    for hops in (0, 1, list_revision._ANCESTOR_HOPS, 20):
        monkeypatch.setattr(list_revision, "_ANCESTOR_HOPS", hops)
        depths.add(_paren_depth(list_revision._build_trigger_sql()))
    assert len(depths) == 1, depths


@pytest.mark.parametrize("internal_hops, extra, internal", [
    (0, None, True), (1, None, True), (6, None, True), (7, None, False),  # deeper than _ANCESTOR_HOPS: shown
    (2, "visible", False), (2, "dangling", False), (2, "cycle", False), (0, "self", False),
])
def test_the_ancestor_walk(store, scope_a, internal_hops, extra, internal):
    with _db(scope_a) as conn:
        parent = None
        if extra == "visible":
            conn.execute("INSERT INTO sessions(id, source, started_at) VALUES ('top', 'desktop', 0)")
            parent = "top"
        elif extra == "dangling":
            parent = "gone"
        for hop in range(internal_hops):
            conn.execute("INSERT INTO sessions(id, source, started_at, parent_session_id) VALUES (?, 'cron', 0, ?)",
                         (f"a{hop}", parent))
            parent = f"a{hop}"
        if extra == "cycle":
            conn.execute("UPDATE sessions SET parent_session_id = ? WHERE id = 'a0'", (parent,))
        conn.execute("INSERT INTO sessions(id, source, started_at, parent_session_id) VALUES ('leaf', 'cron', 0, ?)",
                     ("leaf" if extra == "self" else parent,))
        row = conn.execute(f"SELECT {list_revision._ancestors_internal('s')} FROM sessions s WHERE id = 'leaf'")
        assert bool(row.fetchone()[0]) is internal


def _old_sqlite_runners():
    """(label, runner) for each SQLite older than 3.46 on this host; runner(db_path, script) -> (ok, output)."""
    import shutil
    import subprocess

    runners = []
    cli = shutil.which("sqlite3")
    if cli:
        version = subprocess.run([cli, "--version"], capture_output=True, text=True, check=False).stdout.split()[:1]
        if version and tuple(int(p) for p in version[0].split(".")[:3]) < _OLD_SQLITE:
            def run_cli(path, script, cli=cli):
                done = subprocess.run([cli, "-bail", str(path)], input=script, capture_output=True, text=True, check=False)
                return done.returncode == 0 and not done.stderr, done.stdout + done.stderr
            runners.append((f"sqlite3 CLI {version[0]}", run_cli))
    py = "/usr/bin/python3"
    if os.path.exists(py):
        version = subprocess.run([py, "-c", "import sqlite3; print(sqlite3.sqlite_version)"], capture_output=True,
                                 text=True, check=False).stdout.strip()
        if version and tuple(int(p) for p in version.split(".")[:3]) < _OLD_SQLITE:
            code = ("import sqlite3, sys; c = sqlite3.connect(sys.argv[1], isolation_level=None);"
                    " c.executescript(sys.stdin.read()); print('ok')")

            def run_py(path, script, py=py, code=code):
                done = subprocess.run([py, "-c", code, str(path)], input=script, capture_output=True, text=True,
                                      check=False)
                return done.returncode == 0, done.stdout + done.stderr
            runners.append((f"{py} (SQLite {version})", run_py))
    return runners


def test_every_generated_ddl_statement_parses_on_an_old_sqlite(store, scope_a, tmp_path):
    runners = _old_sqlite_runners()
    if not runners:
        pytest.skip("no SQLite older than 3.46 on this host (system sqlite3 CLI or /usr/bin/python3): only the "
                    "portable nesting guard runs")
    from persistence.sqlite._schema_ddl import DEFERRED_INDEX_SQL, SCHEMA_SQL

    _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, ?)",
         (POLICY_PREFIX + "chat", json.dumps({"lineage": ["chat", "gone"]})))()
    for label, run in runners:
        # A store this build wrote (triggers + receipt index in sqlite_master): the old build reads it ...
        ok, out = run(store_for(scope_a), "SELECT COUNT(*) FROM sessions;\nPRAGMA quick_check;\n")
        assert ok and "ok" in out, (label, out)
        # ... and creates every statement itself (a Hermes process linked to it opens writable), triggers firing.
        fresh = tmp_path / f"fresh-{len(label)}.db"
        script = (SCHEMA_SQL + "\n" + DEFERRED_INDEX_SQL
                  + "\nINSERT INTO sessions(id, source, started_at) VALUES ('w', 'cron', 0);"
                  + "\nINSERT INTO sessions(id, source, started_at, parent_session_id) VALUES ('v', 'desktop', 0, 'w');"
                  + "\nINSERT INTO messages(session_id, role, content, timestamp) VALUES ('w', 'user', 'x', 1);\n")
        ok, out = run(fresh, script)
        assert ok, (label, out)
        current = tmp_path / f"current-{len(label)}.db"
        with closing(sqlite3.connect(current, isolation_level=None)) as conn:
            conn.executescript(script)  # the same script on this interpreter's (current) SQLite
            expected = list_revision.visible_list_revision(conn)[0]
        with closing(sqlite3.connect(fresh)) as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
            assert set(list_revision.TRIGGER_NAMES) <= names, (label, names)
            assert list_revision.visible_list_revision(conn)[0] == expected > 0, label  # same bumps on both engines


# --- A rebuild of a table the triggers name never leaves them dangling (integrated review #2, finding 2) ----------
def _visible_triggers(conn):
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE '%visible_list_rev%'")}


@pytest.mark.parametrize("legacy_alter_table", [False, True])
def test_a_sessions_rebuild_drops_the_triggers_and_writes_keep_working(store, scope_a, legacy_alter_table):
    # legacy_alter_table=OFF is the review's probe2 shape: the RENAME rewrites every trigger body naming sessions
    # to sessions_legacy. It also rewrites the child tables' REFERENCES and five pre-existing messages triggers
    # (transcript/display revision, FTS trigram), so a messages write there fails whatever add45 does: a real
    # sessions rebuild has to run with legacy_alter_table=ON (as _schema's FTS renames do), the second case, where
    # the store's own message append must keep working. state_meta is only named by add45's triggers: both cases.
    from persistence.sqlite._schema import _rebuild_table

    with _db(scope_a) as conn:
        conn.isolation_level = None
        # An older version's trigger naming sessions goes too; an unrelated trigger stays.
        conn.execute("CREATE TRIGGER messages_visible_list_rev_v0_insert AFTER INSERT ON messages "
                     "WHEN (SELECT 1 FROM sessions WHERE id = new.session_id) BEGIN SELECT 1; END")
        conn.execute("CREATE TRIGGER unrelated_audit AFTER INSERT ON messages BEGIN SELECT 1; END")
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sessions'").fetchone()[0]
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(f"PRAGMA legacy_alter_table = {'ON' if legacy_alter_table else 'OFF'}")
        _rebuild_table(conn.cursor(), "sessions", "sessions_legacy", ddl,
                       "INSERT INTO sessions SELECT * FROM sessions_legacy")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        assert _visible_triggers(conn) == set()
        assert "unrelated_audit" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        conn.execute("DROP TRIGGER unrelated_audit")
    assert _revision(scope_a) is None  # readers fall back to the stat signature
    for key in (POLICY_PREFIX + "chat", "other"):  # a receipt (the state_meta triggers' own path) and a plain key
        _sql(scope_a, "INSERT INTO state_meta(key, value) VALUES (?, ?)",
             (key, json.dumps({"lineage": ["chat", "job"]})))()
    if legacy_alter_table:
        store.messages.append("chat", NewMessage(role="user", content="after the rebuild"))
        store.messages.append("job", NewMessage(role="user", content="after the rebuild"))
    from persistence.sqlite._holder import StoreHolder

    StoreHolder(store_for(scope_a)).close()  # the next writable open re-creates the set
    with _db(scope_a) as conn:
        assert _visible_triggers(conn) == set(list_revision.TRIGGER_NAMES)
    assert _revision(scope_a) is not None


def test_a_rebuild_of_another_table_keeps_the_triggers(store, scope_a):
    from persistence.sqlite._schema import _rebuild_table

    with _db(scope_a) as conn:
        conn.isolation_level = None
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'session_model_usage'").fetchone()[0]
        _rebuild_table(conn.cursor(), "session_model_usage", "session_model_usage_legacy", ddl,
                       "INSERT INTO session_model_usage SELECT * FROM session_model_usage_legacy")
        assert _visible_triggers(conn) == set(list_revision.TRIGGER_NAMES)


def test_dropping_them_first_lets_a_column_drop_through(store, scope_a):
    # No migration drops or renames a column of sessions/messages/state_meta today; one that does must call
    # drop_visible_list_triggers first, as _rebuild_table does (SQLite refuses the DROP COLUMN otherwise).
    with _db(scope_a) as conn:
        conn.isolation_level = None
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions DROP COLUMN end_reason")
        conn.execute("BEGIN IMMEDIATE")
        assert set(list_revision.drop_visible_list_triggers(conn.cursor(), "sessions")) == set(
            list_revision.TRIGGER_NAMES)
        conn.execute("ALTER TABLE sessions DROP COLUMN end_reason")
        conn.execute("ROLLBACK")
        assert _visible_triggers(conn) == set(list_revision.TRIGGER_NAMES)


def test_a_recovery_destination_is_created_without_the_triggers(tmp_path):
    from persistence.sqlite.admin._recovery import _fresh_destination

    conn = _fresh_destination(tmp_path / "recovered.db")
    try:
        assert _visible_triggers(conn) == set()
        assert list_revision.visible_list_revision(conn) is None
    finally:
        conn.close()
