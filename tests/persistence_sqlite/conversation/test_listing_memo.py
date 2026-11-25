"""Browse handles answer a listing of a store that has not moved from the listing memo, and never answer one
across a commit, a racy (same-tick) mtime, a writer handle or an open read generation."""

import json
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from persistence.conversation.sessions import NewSession, SessionCountQuery, SessionQuery, SessionWindow
from persistence.sqlite._holder import StoreHolder
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
    return [str(e.session.session_id) for e in handle.sessions.list_sessions(
        SessionQuery(), SessionWindow(**window), compact_rows=True)]


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


def test_store_realpath_is_shared_by_fresh_readers_but_not_across_retarget(store, backend, scope_a, scope_b):
    memo = _listing_memo()
    _quiet(scope_a)
    other = backend.open(scope_b)
    other.sessions.create(NewSession("other", "cli"))
    link = store_for(scope_a).with_name("link.db")
    link.symlink_to(store_for(scope_a))
    resolutions = []

    def listed():
        with closing(StoreHolder(link, read_only=True)) as holder:
            def read():
                with holder._read_ctx() as conn:
                    return tuple(row[0] for row in conn.execute("SELECT id FROM sessions ORDER BY id"))

            with patch.object(memo, "os", wraps=os) as calls:
                answer = memo.memoized(holder, "ids", (), read)
                resolutions.extend(calls.path.realpath.call_args_list)
                return answer

    for _ in range(50):
        assert listed() == ("a", "b", "c")
    assert len(resolutions) == 1
    link.unlink()
    link.symlink_to(store_for(scope_b))
    assert listed() == ("other",)
    assert len(resolutions) == 2
    link.unlink()
    assert memo.store_signature(link) is None
    assert memo._memo_store(_holder(db_path=link)) == os.path.realpath(link)


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
        SessionQuery(sources=("telegram",)), SessionWindow(limit=10), compact_rows=True)])
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
    import copy

    _write(scope_a, "UPDATE sessions SET model_config=?, origin_json=? WHERE id='a'",
           (json.dumps({"k": {"nested": [1, 2]}}), json.dumps({"platform": "cli"})))
    _quiet(scope_a)

    def entries(r):
        return r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10), compact_rows=True)

    rows = _browse(scope_a, lambda r: [e.as_list_row() for e in entries(r)])
    for row in rows:  # a caller's row dicts are its own
        row["title"] = "mutated"
        row.clear()
    kept = {str(e.session.session_id): e for e in _browse(scope_a, entries)}
    assert sorted(kept) == ["a", "b", "c"]
    assert all(e.as_list_row().get("title") != "mutated" for e in kept.values())
    # The shared entry's mappings read, encode and copy like dicts, and refuse mutation.
    config, origin = kept["a"].session.model_config, kept["a"].session.origin
    assert isinstance(config, dict) and config["k"]["nested"] == [1, 2] and origin == {"platform": "cli"}
    assert json.loads(json.dumps(config)) == {"k": {"nested": [1, 2]}}
    for mutate in (lambda: config.__setitem__("x", 1), lambda: config["k"].update(x=1),
                   lambda: config["k"]["nested"].append(3), lambda: origin.pop("platform")):
        with pytest.raises(TypeError):
            mutate()
    mine = copy.deepcopy(config)
    mine["k"]["nested"].append(3)
    assert type(mine) is dict and config["k"]["nested"] == [1, 2]
    assert dict(config) == {"k": {"nested": [1, 2]}}
    again = {str(e.session.session_id): e for e in _browse(scope_a, entries)}
    assert again["a"].session.model_config == {"k": {"nested": [1, 2]}}


def test_full_rows_are_never_kept(store, scope_a, monkeypatch):
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    _quiet(scope_a)
    for _ in range(3):
        _browse(scope_a, lambda r: r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10)))
    assert len(reads) == 3


def test_a_kept_answer_expires(store, scope_a, monkeypatch):
    """A WAL commit is published in -shm after its fsync, which moves no stat: kept answers are bounded in age."""
    reads = _count_calls(monkeypatch, sessions_module, "list_sessions_rich")
    _quiet(scope_a)
    _browse(scope_a, lambda r: _listed(r, limit=10))
    _browse(scope_a, lambda r: _listed(r, limit=10))
    assert len(reads) == 1
    monkeypatch.setattr(_listing_memo(), "_MAX_AGE_S", 0.0)
    _browse(scope_a, lambda r: _listed(r, limit=10))
    assert len(reads) == 2


def test_a_symlinked_store_sees_commits_written_beside_its_target(backend, scope_a, scope_b):
    from persistence_assembly import profile_scope

    writer = backend.open(scope_b)
    for sid in ("x", "y"):
        writer.sessions.create(NewSession(sid, "cli"))
    linked = store_for(scope_a).parent.parent / "linked"
    linked.mkdir()
    (linked / "state.db").symlink_to(store_for(scope_b))

    def browse_link():
        with open_persistence_reader(profile_scope(home=linked).profile_dir) as reader:
            return sorted(_listed(reader, limit=10))

    _quiet(scope_b)
    assert browse_link() == ["x", "y"]
    writer.sessions.create(NewSession("z", "cli"))  # its WAL frames land beside the TARGET
    assert browse_link() == ["x", "y", "z"]


def test_a_handle_still_on_a_replaced_store_file_never_feeds_the_memo(backend, scope_a, scope_b):
    first = backend.open(scope_a)
    for sid in ("a", "b"):
        first.sessions.create(NewSession(sid, "cli"))
    other = backend.open(scope_b)
    other.sessions.create(NewSession("x", "cli"))
    path = store_for(scope_a)
    with open_persistence_reader(path) as old_handle:
        assert sorted(_listed(old_handle, limit=10)) == ["a", "b"]  # its connection is on the old file
        backend.close()
        replacement = path.with_name("replacement.db")
        with closing(sqlite3.connect(store_for(scope_b))) as src, closing(sqlite3.connect(replacement)) as dst:
            src.backup(dst)
        os.replace(replacement, path)
        for sidecar in (path.with_name("state.db-wal"), path.with_name("state.db-shm")):
            sidecar.unlink(missing_ok=True)
        _quiet(scope_a)
        try:
            _listed(old_handle, limit=10)  # still the old file (or refused): either way, not kept
        except Exception:
            pass
    assert _browse(scope_a, lambda r: _listed(r, limit=10)) == ["x"]


def _write(scope, sql, params=()):
    with sqlite3.connect(store_for(scope)) as connection:
        connection.execute(sql, params)


def test_the_local_lineage_projection_is_shared_until_a_lineage_write(store, scope_a, monkeypatch):
    store.sessions.create(NewSession("owner", "cli"))
    store.sessions.create(NewSession("head", "cli"))
    _write(scope_a, "UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
    _write(scope_a, "INSERT INTO state_meta(key,value) VALUES(?,?)",
           (POLICY_PREFIX + "owner", json.dumps({"session_id": "owner", "lineage": ["owner", "head"]})))
    # A store without idx_state_meta_local_lineage (an indexed one seeks, nothing to share: add45).
    _write(scope_a, "DROP INDEX IF EXISTS idx_state_meta_local_lineage")
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
                             _pinned_read_conn=lambda: None, _db_file_identity=None)
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
    assert sig is not None and sig[2] == ()  # no -wal
    assert sig[0] == (os.stat(db).st_dev, os.stat(db).st_ino)
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


def test_a_holder_whose_file_identity_is_unknown_never_uses_the_memo(monkeypatch, tmp_path):
    listing_memo = _listing_memo()
    monkeypatch.setattr(listing_memo, "_now_ns", lambda: time.time_ns() + 10_000_000_000)
    db = tmp_path / "state.db"
    db.write_bytes(b"x" * 10)
    st = os.stat(db)
    computed = []

    def compute():
        computed.append(1)
        return 7

    for identity, reads in ((None, 2), ((st.st_dev, st.st_ino + 1), 2), ((st.st_dev, st.st_ino), 1)):
        listing_memo.clear()
        computed.clear()
        holder = _holder(db_path=str(db), _db_file_identity=identity)
        assert [listing_memo.memoized(holder, "k", (), compute) for _ in range(2)] == [7, 7]
        assert len(computed) == reads, identity


def test_only_answers_kept_while_the_wal_held_frames_expire(monkeypatch):
    listing_memo = _listing_memo()
    monkeypatch.setattr(listing_memo, "_MAX_AGE_S", 15.0)
    db_parts = (1, 2, 3, 4, 5)
    no_wal, with_wal = ((1, 4), db_parts, ()), ((1, 4), db_parts, (1, 9, 9, 9, 9))
    for signature, expires in ((no_wal, False), (with_wal, True)):
        kept = (signature, "answer", 1, 100.0)
        assert listing_memo._fresh(kept, signature, 110.0)
        assert listing_memo._fresh(kept, signature, 130.0) is not expires
        assert not listing_memo._fresh(kept, (signature[0], db_parts, (1, 1, 1, 1, 1)), 101.0)


def test_a_read_only_compact_listing_is_immutable_even_when_not_kept(store, scope_a):
    """Racy (just written) stores are read every time; their entries still refuse mutation, so a consumer that
    mutates a listing fails in a test, not only once a store settles in production."""
    _write(scope_a, "UPDATE sessions SET model_config=? WHERE id='a'", (json.dumps({"k": 1}),))
    entries = _browse(scope_a, lambda r: r.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10),
                                                                   compact_rows=True))
    config = next(e for e in entries if str(e.session.session_id) == "a").session.model_config
    assert config == {"k": 1}
    with pytest.raises(TypeError):
        config["k"] = 2
    # A writer's own listings keep their plain mappings.
    mine = next(e for e in store.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10), compact_rows=True)
                if str(e.session.session_id) == "a")
    mine.session.model_config["k"] = 2
