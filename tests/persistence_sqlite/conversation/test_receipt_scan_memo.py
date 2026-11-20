"""apexfix: memoized retire_prunable receipt scan (``_store/receipt_scan_memo.py``).

The memoized scans must return exactly what the full receipt scans return, after any write.
"""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from persistence.sqlite.conversation._store import receipt_scan_memo as memo_mod
from persistence.sqlite.conversation._store.local import POLICY_PREFIX, retire_local_receipts
from persistence.sqlite.conversation._store.retirement import _local_conversations

P = POLICY_PREFIX


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.delenv(memo_mod.KILL_SWITCH_ENV, raising=False)
    c = sqlite3.connect(tmp_path / "state.db", isolation_level=None)
    c.execute("CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, runtime_revision INTEGER NOT NULL DEFAULT 0)")
    yield c
    c.close()


def _receipt(sid, *, lineage=None, entry=None, request_id="cron:job:x", pad=0):
    body = {"session_id": sid, "request_id": request_id, "policy": {"config_json": "x" * pad}}
    if lineage is not None:
        body["lineage"] = lineage
    if entry is not None:
        body["entry"] = {"session_id": entry}
    return json.dumps(body)


def _put(conn, sid, raw):
    conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (P + sid, raw))
    conn.execute("INSERT OR IGNORE INTO sessions(id) VALUES(?)", (sid,))


def _full(conn, monkeypatch):
    monkeypatch.setenv(memo_mod.KILL_SWITCH_ENV, "0")
    try:
        conversations = _local_conversations(conn)
        conn.execute("BEGIN")
        retire_local_receipts(conn, _all_ids(conn))
        gone = _gone(conn)
        conn.execute("ROLLBACK")
        return conversations, gone
    finally:
        monkeypatch.delenv(memo_mod.KILL_SWITCH_ENV)


def _memo(conn, *, committed=True):
    conversations = _local_conversations(conn, committed=committed)
    conn.execute("BEGIN")
    retire_local_receipts(conn, _all_ids(conn))
    gone = _gone(conn)
    conn.execute("ROLLBACK")
    return conversations, gone


def _all_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM sessions")][::3]  # a third of them are "deleted"


def _gone(conn):
    """The receipt keys left after ``retire_local_receipts`` (inside the caller's open transaction)."""
    return sorted(r[0] for r in conn.execute("SELECT key FROM state_meta WHERE key GLOB ?", (P + "*",)))


def _seed(conn, n=120, seed=7):
    rng = random.Random(seed)
    for i in range(n):
        sid = f"s{i:04d}"
        kind = rng.choice(["plain", "lineage", "derived", "entry", "overlap"])
        if kind == "lineage":
            raw = _receipt(sid, lineage=[sid, f"{sid}c1", f"{sid}c2"], entry=f"{sid}c2")
        elif kind == "derived":
            raw = _receipt(sid, request_id=rng.choice(["bot-chat:apex:1", "a2a:x"]), entry=sid)
        elif kind == "entry":
            raw = _receipt(sid, entry=sid, pad=rng.randint(0, 400))
        elif kind == "overlap":  # an id named by two receipts: later key wins, as in the scan
            raw = _receipt(sid, lineage=[sid, "s0001"], entry="s0001")
        else:
            raw = _receipt(sid)
        _put(conn, sid, raw)
    _put(conn, "broken", "{not json")
    _put(conn, "array", "[1,2]")
    _put(conn, "nonstr", json.dumps({"session_id": 5, "lineage": "nope", "entry": [1]}))


def test_memo_matches_full_scan_across_writes(conn, monkeypatch):
    _seed(conn)
    assert _memo(conn) == _full(conn, monkeypatch)  # cold
    assert _memo(conn) == _full(conn, monkeypatch)  # warm

    # insert
    _put(conn, "s9000", _receipt("s9000", lineage=["s9000", "s0002"], entry="s0002"))
    assert _memo(conn) == _full(conn, monkeypatch)
    # delete
    conn.execute("DELETE FROM state_meta WHERE key=?", (P + "s0003",))
    assert _memo(conn) == _full(conn, monkeypatch)
    # in-place lineage rewrite of the SAME byte length, through the production writer's contract
    old = conn.execute("SELECT value FROM state_meta WHERE key=?", (P + "s0004",)).fetchone()[0]
    new = old.replace('"s0004"', '"s0004"', 1)
    body = json.loads(old)
    body["entry"] = {"session_id": "zzzzz"}
    new = json.dumps(body)
    conn.execute("BEGIN")
    conn.execute("UPDATE state_meta SET value=? WHERE key=?", (new, P + "s0004"))
    conn.execute("UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id='s0004'")
    conn.execute("COMMIT")
    assert _memo(conn) == _full(conn, monkeypatch)
    # INSERT OR REPLACE (new rowid)
    conn.execute("INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)",
                 (P + "s0005", _receipt("s0005", request_id="bot-chat:x", entry="s0006")))
    assert _memo(conn) == _full(conn, monkeypatch)
    # length-changing rewrite without a revision bump
    conn.execute("UPDATE state_meta SET value=? WHERE key=?",
                 (_receipt("s0007", lineage=["s0007", "s0008", "s0009"], entry="s0009"), P + "s0007"))
    assert _memo(conn) == _full(conn, monkeypatch)
    # re-key (UPDATE key keeps the rowid)
    conn.execute("UPDATE state_meta SET key=? WHERE key=?", (P + "s0010x", P + "s0010"))
    assert _memo(conn) == _full(conn, monkeypatch)
    # many moved rows at once (> _REPARSE_ONE_BY_ONE): the bulk path
    for i in range(200, 300):
        _put(conn, f"t{i}", _receipt(f"t{i}", lineage=[f"t{i}", f"s{i % 50:04d}"], entry=f"t{i}"))
    assert _memo(conn) == _full(conn, monkeypatch)


def test_only_moved_receipts_are_parsed(conn, monkeypatch):
    _seed(conn, n=300)
    _memo(conn)
    parsed = []
    real = memo_mod.project
    monkeypatch.setattr(memo_mod, "project", lambda raw: parsed.append(raw) or real(raw))
    _memo(conn)
    assert parsed == []  # warm: nothing re-parsed
    _put(conn, "new1", _receipt("new1", entry="new1"))
    _memo(conn)
    # The new receipt, parsed by _local_conversations (remembered) and then served from the memo.
    assert len(parsed) == 1


def test_uncommitted_receipts_are_never_remembered(conn, monkeypatch):
    _seed(conn, n=20)
    _memo(conn)
    conn.execute("BEGIN")
    _put(conn, "ghost", _receipt("ghost", lineage=["ghost", "s0001"], entry="aaaaa"))
    in_txn = memo_mod.receipt_projections(conn, remember=False)
    assert dict(in_txn)[P + "ghost"][2] == "aaaaa"
    conn.execute("ROLLBACK")
    # Same key, same rowid and same byte length, different projection: must not be served stale.
    _put(conn, "ghost", _receipt("ghost", lineage=["ghost", "s0002"], entry="bbbbb"))
    assert dict(memo_mod.receipt_projections(conn, remember=True))[P + "ghost"][2] == "bbbbb"
    assert _memo(conn) == _full(conn, monkeypatch)


def test_kill_switch_takes_the_full_scan(conn, monkeypatch):
    _seed(conn, n=10)
    monkeypatch.setenv(memo_mod.KILL_SWITCH_ENV, "0")
    called = []
    monkeypatch.setattr(memo_mod, "receipt_projections", lambda *a, **k: called.append(1) or [])
    _local_conversations(conn, committed=True)
    conn.execute("BEGIN")
    retire_local_receipts(conn, ["s0001"])
    conn.execute("ROLLBACK")
    assert called == []


def test_retire_local_receipts_deletes_the_same_keys(conn, monkeypatch):
    _seed(conn, n=60)
    targets = ["s0001", "s0002c2", "s0005", "nope"]

    def run():
        conn.execute("BEGIN")
        retire_local_receipts(conn, targets)
        left = sorted(r[0] for r in conn.execute("SELECT key FROM state_meta"))
        conn.execute("ROLLBACK")
        return left

    memo_left = run()
    monkeypatch.setenv(memo_mod.KILL_SWITCH_ENV, "0")
    assert run() == memo_left
