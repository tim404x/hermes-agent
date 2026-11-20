"""The delete path's receipt scans without re-parsing every receipt on every delete.

# apexfix: memoized retire_prunable receipt scan

``retirement._local_conversations`` (every prune / empty-session sweep / explicit delete through
``retire_prunable``) and ``local.retire_local_receipts`` (every ``retire_sessions``) each read and
``json.loads`` EVERY ``gateway.local_policy.v1:*`` receipt. A receipt carries its session's whole policy
(~40 KB) and every cron fire leaves one: on a store with 10,493 receipts / 379 MB one scan costs ~1.2 s, so
a single archive or delete in the desktop app took 1.9-7.9 s.

Per store file this keeps what each receipt projects to (the ids of its local conversation, whether it is
a derived Bot Chat / A2A conversation, and its current physical target) under the row's fingerprint, the
same fingerprint ``local_lineage_memo`` uses:

- its key and ``rowid``: an insert, a delete, an ``INSERT OR REPLACE`` or a re-key moves them;
- its byte length (``octet_length``: record header, no body read): any rewrite that changes it;
- its owner session's ``runtime_revision``: the only in-place writer of the projected fields,
  ``local_lineage._commit_local_target`` (lineage + entry), bumps it in the same transaction.

Every other in-place writer of a policy receipt leaves the projected fields alone (the profile re-key in
``housekeeping/profile_identity`` rewrites ``route`` and ``entry.session_key`` / ``entry.origin``, never
``session_id`` / ``lineage`` / ``entry.session_id`` / ``request_id``). A call reads the fingerprints of the
caller's own snapshot (its open transaction included), re-parses only the receipts whose fingerprint moved,
and returns exactly what the full scan returns, in the same key order.

Write-back happens only where the caller says the snapshot is committed data (``retire_prunable``: every
caller selects its ids and calls it before writing anything in that transaction). Elsewhere the memo is
read, never updated, so an uncommitted (and possibly rolled back) receipt can never be remembered.

Kill switch: ``HERMES_RETIRE_SCAN_MEMO=0`` restores the full scan. SQLite older than 3.43 (no
``octet_length``) and a connection without a file take the full scan too.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import OrderedDict
from typing import Any, Iterator, Optional

KILL_SWITCH_ENV = "HERMES_RETIRE_SCAN_MEMO"
_MAX_STORES = 16
_OCTET_LENGTH_SQLITE = (3, 43, 0)
# More moved receipts than this: one pass over all of them beats that many keyed reads.
_REPARSE_ONE_BY_ONE = 64


def _prefix() -> str:
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX
    return POLICY_PREFIX


_FINGERPRINTS_SQL = "SELECT key, rowid, octet_length(value) FROM state_meta WHERE key GLOB ? ORDER BY key"
_ALL_SQL = "SELECT key, rowid, octet_length(value), value FROM state_meta WHERE key GLOB ? ORDER BY key"
_ONE_SQL = "SELECT rowid, octet_length(value), value FROM state_meta WHERE key = ?"

_MISSING = object()


class _Memo:
    __slots__ = ("has_revision", "lock", "rows")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # key -> (rowid, byte length, owner runtime_revision, projection), in key order
        self.rows: dict[str, tuple] = {}
        self.has_revision: Optional[bool] = None


_stores: "OrderedDict[tuple, _Memo]" = OrderedDict()
_stores_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip() != "0"


def project(raw: Any) -> tuple:
    """``(ids, derived, target)`` exactly as the full scans read one receipt.

    ``ids``/``derived``: ``retirement._local_conversations`` (None ids = the scan skips the receipt).
    ``target``: ``local._policy_target`` (None for a malformed receipt)."""
    from persistence.sqlite.conversation._store.retirement import _DERIVED_LOCAL_PREFIXES
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        return None, False, None
    if not isinstance(receipt, dict):
        return None, False, None
    entry, lineage = receipt.get("entry"), receipt.get("lineage")
    ids = tuple(x for x in [receipt.get("session_id"), *(lineage if isinstance(lineage, list) else []),
                            entry.get("session_id") if isinstance(entry, dict) else None] if isinstance(x, str))
    derived = str(receipt.get("request_id") or "").startswith(_DERIVED_LOCAL_PREFIXES)
    target = entry.get("session_id") if isinstance(entry, dict) else None
    return ids, derived, target


def _store_identity(conn) -> Optional[tuple]:
    """The main database FILE of *conn* (a recreated state.db at the same path is another store)."""
    try:
        row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    except sqlite3.Error:
        return None
    path = row[2] if row else None
    if not path:
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


def _revisions(conn, memo: _Memo) -> dict:
    if memo.has_revision is None:
        memo.has_revision = any(row[1] == "runtime_revision" for row in conn.execute("PRAGMA table_info(sessions)"))
    if not memo.has_revision:
        return {}
    return dict(tuple(row) for row in conn.execute(
        "SELECT id, runtime_revision FROM sessions WHERE runtime_revision <> 0"))


def _full(conn) -> Iterator[tuple[str, tuple]]:
    for key, raw in conn.execute("SELECT key, value FROM state_meta WHERE key GLOB ?", (_prefix() + "*",)):
        yield key, project(raw)


def receipt_projections(conn, *, remember: bool) -> list[tuple[str, tuple]]:
    """``[(key, (ids, derived, target)), ...]`` for every policy receipt in *conn*'s snapshot, key order.

    ``remember``: the snapshot holds only committed receipts (see the module docstring), so moved rows
    may be written back to the memo; otherwise the memo is only read."""
    if not enabled() or sqlite3.sqlite_version_info < _OCTET_LENGTH_SQLITE:
        return list(_full(conn))
    store = _store_identity(conn)
    if store is None:
        return list(_full(conn))
    memo = _memo_for(store)
    prefix = _prefix()
    with memo.lock:
        revisions = _revisions(conn, memo)
        saved = memo.rows
        rows: dict[str, tuple] = {}
        moved: list[str] = []
        for key, rowid, length in conn.execute(_FINGERPRINTS_SQL, (prefix + "*",)):
            fingerprint = (rowid, length, revisions.get(key[len(prefix):], 0))
            held = saved.get(key)
            if held is not None and held[:3] == fingerprint:
                rows[key] = held
            else:
                rows[key] = fingerprint + (_MISSING,)
                moved.append(key)
        if len(moved) > _REPARSE_ONE_BY_ONE:
            fresh = {key: (rowid, length, revisions.get(key[len(prefix):], 0), project(raw))
                     for key, rowid, length, raw in conn.execute(_ALL_SQL, (prefix + "*",))}
            rows = fresh
        else:
            for key in moved:
                found = conn.execute(_ONE_SQL, (key,)).fetchone()
                if found is None:  # cannot happen inside one snapshot; stay exact anyway
                    del rows[key]
                else:
                    rows[key] = (found[0], found[1], rows[key][2], project(found[2]))
        if remember:
            memo.rows = rows
    return [(key, entry[3]) for key, entry in rows.items()]


def warm(conn) -> None:
    """Teach the memo *conn*'s receipts. Only for a caller that has written no receipt in its open
    transaction yet (explicit deletes call it right before ``retire_sessions``), so the remembered
    rows are committed ones; ``retire_local_receipts`` then serves from the memo."""
    if enabled():
        receipt_projections(conn, remember=True)
