"""The local lineage index without re-parsing every receipt on every listing.

``hermes_state_local.local_lineage_index`` finds the receipts whose lineage has more than one segment
by JSON-parsing EVERY ``gateway.local_policy.v1:*`` row. A receipt carries its session's whole policy
(about 36 KB) and every cron run leaves one: a profile with 8,900 receipts parses 317 MB of JSON, 0.4 s
of CPU, per session listing. Its per-connection reuse keys on ``PRAGMA data_version``, which any commit
to a busy store moves, and the roster's ``profiles.list`` (every 5 s) and the REST routes open a fresh
handle per request anyway.

Per store file this keeps what each receipt last parsed to (its lineage, when it has more than one
segment) under the row's fingerprint:

- its ``rowid``: an insert, a delete or an ``INSERT OR REPLACE`` moves it;
- its byte length: any in-place rewrite that changes it;
- its owner session's ``runtime_revision``: every lineage write (``_commit_local_target``) bumps it in
  the same transaction, so a rewrite that happens to keep the length still moves the fingerprint.

A listing reads the fingerprints (index and record headers, no bodies), re-parses only the receipts whose
fingerprint moved, and reads the segments' ``end_reason`` fresh, exactly as the full scan does. All of it
in one read transaction, so the answer is one snapshot's.
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from typing import Any, Optional

from hermes_state_local import POLICY_PREFIX, local_lineage_index

_MAX_STORES = 16
# More moved receipts than this: one pass over all of them beats that many keyed reads.
_REPARSE_ONE_BY_ONE = 64

# The receipt's body only when the full scan would have selected it (nested CASE: evaluated in order).
_MULTI_SEGMENT = ("CASE WHEN json_valid(value) THEN CASE WHEN json_array_length(value, '$.lineage') > 1 "
                  "THEN value END END")
_ALL_SQL = f"SELECT key, rowid, octet_length(value), {_MULTI_SEGMENT} FROM state_meta WHERE key GLOB ? ORDER BY key"
_FINGERPRINTS_SQL = "SELECT key, rowid, octet_length(value) FROM state_meta WHERE key GLOB ? ORDER BY key"
_ONE_SQL = f"SELECT rowid, octet_length(value), {_MULTI_SEGMENT} FROM state_meta WHERE key = ?"


class _Memo:
    __slots__ = ("has_revision", "lock", "rows")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # receipt key -> (rowid, byte length, owner runtime_revision, (owner, lineage) or None), in key order
        self.rows: Optional[dict[str, tuple]] = None
        self.has_revision: Optional[bool] = None


_stores: OrderedDict[tuple, _Memo] = OrderedDict()
_stores_lock = threading.Lock()


def _store_identity(db: Any) -> Optional[tuple]:
    """The store FILE this handle reads: a recovered or recreated state.db at the same path is another."""
    path = getattr(db, "db_path", None)
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(path), stat.st_dev, stat.st_ino


def _memo_for(store: tuple) -> _Memo:
    with _stores_lock:
        memo = _stores.get(store)
        if memo is None:
            memo = _stores[store] = _Memo()
            while len(_stores) > _MAX_STORES:
                _stores.popitem(last=False)
        else:
            _stores.move_to_end(store)
        return memo


def _parsed(raw: Any) -> Optional[tuple]:
    """``(owner, lineage)`` as ``local_lineage_index`` reads a selected receipt, else None."""
    if raw is None:
        return None
    receipt = json.loads(raw)
    owner, lineage = receipt.get("session_id"), receipt.get("lineage")
    if not isinstance(owner, str) or not all(isinstance(sid, str) for sid in lineage):
        return None
    return owner, tuple(lineage)


def _revisions(conn, memo: _Memo) -> dict:
    if memo.has_revision is None:
        memo.has_revision = any(row[1] == "runtime_revision" for row in conn.execute("PRAGMA table_info(sessions)"))
    if not memo.has_revision:  # a legacy store: rowid and length are all there is
        return {}
    return dict(tuple(row) for row in conn.execute("SELECT id, runtime_revision FROM sessions WHERE runtime_revision <> 0"))


def _all_receipts(conn, revisions: dict) -> dict[str, tuple]:
    prefix = len(POLICY_PREFIX)
    return {key: (rowid, length, revisions.get(key[prefix:], 0), _parsed(raw))
            for key, rowid, length, raw in conn.execute(_ALL_SQL, (POLICY_PREFIX + "*",))}


def _refresh(conn, memo: _Memo) -> list[tuple]:
    """Bring *memo* to *conn*'s snapshot; the multi-segment receipts in key order (the scan's order)."""
    revisions = _revisions(conn, memo)
    if memo.rows is None:
        memo.rows = _all_receipts(conn, revisions)
    else:
        prefix, saved, rows, moved = len(POLICY_PREFIX), memo.rows, {}, []
        for key, rowid, length in conn.execute(_FINGERPRINTS_SQL, (POLICY_PREFIX + "*",)):
            fingerprint = (rowid, length, revisions.get(key[prefix:], 0))
            held = saved.get(key)
            if held is not None and held[:3] == fingerprint:
                rows[key] = held
            else:
                rows[key] = fingerprint
                moved.append(key)
        if len(moved) > _REPARSE_ONE_BY_ONE:
            rows = _all_receipts(conn, revisions)
        else:
            for key in moved:
                found = conn.execute(_ONE_SQL, (key,)).fetchone()
                if found is None:
                    del rows[key]
                else:
                    rows[key] = (found[0], found[1], rows[key][2], _parsed(found[2]))
        memo.rows = rows
    return [entry[3] for entry in memo.rows.values() if entry[3] is not None]


def _index(conn, receipts: list[tuple]) -> tuple[set, dict]:
    """``local_lineage_index``'s projection of the selected receipts; ``end_reason`` is read now."""
    superseded: set = set()
    heads: dict = {}
    for owner, segments in receipts:
        lineage = list(segments)
        marks = ",".join("?" * len(lineage))
        ended = dict(tuple(row) for row in conn.execute(
            f"SELECT id,end_reason FROM sessions WHERE id IN ({marks})", lineage).fetchall())
        starts = [sid for i, sid in enumerate(lineage) if i == 0 or ended.get(lineage[i - 1]) == "session_reset"]
        superseded.update(starts[:-1])
        heads[starts[-1]] = (owner, lineage)
    return superseded, heads


def memoized_local_lineage_index(db: Any, conn) -> tuple[set, dict]:
    """``local_lineage_index(conn)``, parsing only the receipts that moved since this store was last read.
    The caller's own open transaction (uncommitted writes) and a store without a file take the full scan."""
    store = None if conn.in_transaction else _store_identity(db)
    if store is None:
        return local_lineage_index(conn)
    memo = _memo_for(store)
    conn.execute("BEGIN")  # the fingerprints, the bodies they select and end_reason: one snapshot
    try:
        with memo.lock:  # one refresh per store at a time: a cold store is scanned once, not per caller
            receipts = _refresh(conn, memo)
        return _index(conn, receipts)
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
