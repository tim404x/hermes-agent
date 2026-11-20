"""Browse handles answer a listing of a store that has not moved from the listing memo, and never answer one
across a commit, a racy (same-tick) mtime, a writer handle or an open read generation."""

import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from persistence.conversation.sessions import NewSession, SessionCountQuery, SessionQuery, SessionWindow
from persistence.sqlite.conversation import sessions as sessions_module
from persistence.sqlite.conversation._store import local_lineage_memo
from persistence.sqlite.conversation._store.local import POLICY_PREFIX
from persistence_assembly import open_persistence_reader
from tests.persistence_sqlite.conftest import store_for

@pytest.fixture(autouse=True)
def _fresh_memo(monkeypatch):
    monkeypatch.delenv("HERMES_LISTING_MEMO", raising=False)
    try:
        clear = _listing_memo().clear
    except ImportError:  # a tree without the memo (the RED run)
        clear = lambda: None
    clear()
    yield
    clear()


def _listing_memo():
    from persistence.sqlite.conversation._store import listing_memo

    return listing_memo


_QUIET = {"on": False}


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    """``_quiet()``: the memo's clock reads 10 s later from then on, so every store file looks settled (past the
    racy window) without backdating it; a real commit still moves mtime, ctime and size."""
    _QUIET["on"] = False
    try:
        memo = _listing_memo()
    except ImportError:
        yield
        return
    monkeypatch.setattr(memo, "_now_ns", lambda: time.time_ns() + (10_000_000_000 if _QUIET["on"] else 0))
    yield


@pytest.fixture
def store(backend, scope_a):
    store = backend.open(scope_a)
    for sid in ("a", "b", "c"):
        store.sessions.create(NewSession(sid, "cli"))
    return store


def _quiet(scope):
    """The store has been quiet for a while: its files are past the racy window."""
    _QUIET["on"] = True


def _count_calls(monkeypatch, module, name):
    calls = []
    real = getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def _listed(handle, **window):
    return [str(e.session.session_id) for e in handle.sessions.list_sessions(SessionQuery(), SessionWindow(**window))]


def _browse(scope, fn):
    with open_persistence_reader(store_for(scope)) as reader:
        return fn(reader)


def test_a_quiet_store_is_read_once_across_browse_handles(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    _quiet(scope_a)
    first = _browse(scope_a, lambda r: _listed(r, limit=10))
    for _ in range(3):
        assert _browse(scope_a, lambda r: _listed(r, limit=10)) == first
    assert sorted(first) == ["a", "b", "c"]
    assert len(reads) == 1


def test_a_commit_is_seen_by_the_next_listing(store, scope_a, monkeypatch):
    _quiet(scope_a)
    assert sorted(_browse(scope_a, lambda r: _listed(r, limit=10))) == ["a", "b", "c"]
    store.sessions.create(NewSession("d", "cli"))
    assert sorted(_browse(scope_a, lambda r: _listed(r, limit=10))) == ["a", "b", "c", "d"]
    _QUIET["on"] = False  # and with the real clock (the commit is racy): read, never the kept answer
    store.sessions.create(NewSession("e", "cli"))
    assert sorted(_browse(scope_a, lambda r: _listed(r, limit=10))) == ["a", "b", "c", "d", "e"]


def test_a_commit_after_a_kept_answer_moves_the_signature(store, scope_a):
    _quiet(scope_a)
    assert sorted(_browse(scope_a, lambda r: _listed(r, limit=10))) == ["a", "b", "c"]
    store.sessions.create(NewSession("d", "cli"))
    _quiet(scope_a)  # aged again: the commit must still move (size, mtime) away from the kept signature
    assert sorted(_browse(scope_a, lambda r: _listed(r, limit=10))) == ["a", "b", "c", "d"]


def test_a_racy_store_is_read_every_time(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    # Real clock: the store just committed (its files are younger than the racy window).
    for _ in range(3):
        _browse(scope_a, lambda r: _listed(r, limit=10))
    assert len(reads) == 3


def test_a_writer_handle_never_uses_the_memo(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    _quiet(scope_a)
    for _ in range(3):
        _listed(store, limit=10)
    assert len(reads) == 3


def test_kill_switch_reads_every_time(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    monkeypatch.setenv("HERMES_LISTING_MEMO", "0")
    _quiet(scope_a)
    for _ in range(3):
        _browse(scope_a, lambda r: _listed(r, limit=10))
    assert len(reads) == 3


def test_each_window_and_query_is_its_own_answer(store, scope_a):
    _quiet(scope_a)
    one = _browse(scope_a, lambda r: _listed(r, limit=1))
    two = _browse(scope_a, lambda r: _listed(r, limit=2))
    assert len(one) == 1 and len(two) == 2
    assert _browse(scope_a, lambda r: _listed(r, limit=1)) == one
    hidden = _browse(scope_a, lambda r: [str(e.session.session_id) for e in r.sessions.list_sessions(
        SessionQuery(sources=("telegram",)), SessionWindow(limit=10))])
    assert hidden == []


def test_counts_are_kept_and_follow_commits(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "session_count")
    _quiet(scope_a)
    for _ in range(3):
        assert _browse(scope_a, lambda r: r.sessions.count_sessions(SessionCountQuery())) == 3
    assert len(reads) == 1
    store.sessions.create(NewSession("d", "cli"))
    _quiet(scope_a)
    assert _browse(scope_a, lambda r: r.sessions.count_sessions(SessionCountQuery())) == 4


def test_a_kept_answer_cannot_be_mutated_by_a_caller(store, scope_a):
    _quiet(scope_a)
    rows = _browse(scope_a, lambda r: [e.as_list_row() for e in r.sessions.list_sessions(
        SessionQuery(), SessionWindow(limit=10))])
    for row in rows:
        row["title"] = "mutated"
        row.clear()
    again = _browse(scope_a, lambda r: [e.as_list_row() for e in r.sessions.list_sessions(
        SessionQuery(), SessionWindow(limit=10))])
    assert sorted(row["id"] for row in again) == ["a", "b", "c"]
    assert all(row.get("title") != "mutated" for row in again)


def _write(scope, sql, params=()):
    with sqlite3.connect(store_for(scope)) as connection:
        connection.execute(sql, params)


def test_the_local_lineage_projection_is_shared_until_a_lineage_write(store, scope_a, monkeypatch):
    store.sessions.create(NewSession("owner", "cli"))
    store.sessions.create(NewSession("head", "cli"))
    _write(scope_a, "UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
    _write(scope_a, "INSERT INTO state_meta(key,value) VALUES(?,?)",
           (POLICY_PREFIX + "owner", json.dumps({"session_id": "owner", "lineage": ["owner", "head"]})))
    scans = _count_calls(monkeypatch, local_lineage_memo, "memoized_local_lineage_index")
    _quiet(scope_a)
    listed = _browse(scope_a, lambda r: _listed(r, limit=10))
    assert "owner" not in listed and "head" in listed
    _browse(scope_a, lambda r: _listed(r, limit=5))  # another listing shape: same projection
    _browse(scope_a, lambda r: r.sessions.count_sessions(SessionCountQuery(exclude_children=True)))
    assert len(scans) == 1
    # A lineage write (the receipt retired) moves the store: the next listing reprojects.
    _write(scope_a, "DELETE FROM state_meta WHERE key=?", (POLICY_PREFIX + "owner",))
    _quiet(scope_a)
    assert {"owner", "head"} <= set(_browse(scope_a, lambda r: _listed(r, limit=10)))
    assert len(scans) == 2


def _holder(**overrides):
    holder = SimpleNamespace(read_only=True, db_path="/tmp/x/state.db", _conn=None,
                             _pinned_read_conn=lambda: None)
    for key, value in overrides.items():
        setattr(holder, key, value)
    return holder


@pytest.mark.parametrize("overrides", [
    {"read_only": False},
    {"db_path": None},
    {"_pinned_read_conn": lambda: object()},
    {"_conn": SimpleNamespace(in_transaction=True)},
])
def test_handles_that_must_read_their_own_generation_bypass_the_memo(overrides):
    listing_memo = _listing_memo()
    assert listing_memo._memo_store(_holder(**overrides)) is None
    assert listing_memo._memo_store(_holder()) == "/tmp/x/state.db"


def test_signature_ignores_an_empty_wal_and_refuses_a_racy_or_missing_store(tmp_path, monkeypatch):
    listing_memo = _listing_memo()
    clock = {"skew": 0}
    monkeypatch.setattr(listing_memo, "_now_ns", lambda: time.time_ns() + clock["skew"])
    db = tmp_path / "state.db"
    assert listing_memo.store_signature(db) is None  # no store
    db.write_bytes(b"x" * 10)
    assert listing_memo.store_signature(db) is None  # just written: racy
    clock["skew"] = 10_000_000_000
    sig = listing_memo.store_signature(db)
    assert sig is not None and sig[1] == ()  # no -wal
    wal = tmp_path / "state.db-wal"
    wal.write_bytes(b"")
    assert listing_memo.store_signature(db) == sig  # an empty -wal carries no frames
    wal.write_bytes(b"frames")
    moved = listing_memo.store_signature(db)
    assert moved is not None and moved != sig
    clock["skew"] = 0
    assert listing_memo.store_signature(db) is None  # a fresh WAL commit: racy


def test_signature_sees_a_same_size_rewrite_that_restores_the_old_mtime(tmp_path, monkeypatch):
    listing_memo = _listing_memo()
    monkeypatch.setattr(listing_memo, "_now_ns", lambda: time.time_ns() + 10_000_000_000)
    db = tmp_path / "state.db"
    db.write_bytes(b"a" * 10)
    before = os.stat(db)
    sig = listing_memo.store_signature(db)
    time.sleep(0.05)  # a later timestamp tick
    db.write_bytes(b"b" * 10)
    os.utime(db, ns=(before.st_atime_ns, before.st_mtime_ns))  # cp -p / rsync -t put the old mtime back
    assert listing_memo.store_signature(db) != sig  # ctime cannot be backdated


def test_kept_rows_are_bounded(store, scope_a, monkeypatch):
    listing_memo = _listing_memo()
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    monkeypatch.setattr(listing_memo, "_MAX_ROWS", 5)
    _quiet(scope_a)
    _browse(scope_a, lambda r: _listed(r, limit=10))  # 3 rows + 1: kept
    _browse(scope_a, lambda r: _listed(r, limit=2))  # 2 rows + 1: evicts the first (4 + 3 > 5)
    assert len(reads) == 2
    _browse(scope_a, lambda r: _listed(r, limit=2))
    assert len(reads) == 2  # still kept
    _browse(scope_a, lambda r: _listed(r, limit=10))
    assert len(reads) == 3  # was evicted
    assert listing_memo._weight[0] <= 5
    monkeypatch.setattr(listing_memo, "_MAX_ROWS", 2)
    _browse(scope_a, lambda r: _listed(r, limit=9))  # an answer heavier than the whole bound is never kept
    _browse(scope_a, lambda r: _listed(r, limit=9))
    assert len(reads) == 5
