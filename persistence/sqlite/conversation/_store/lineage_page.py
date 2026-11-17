"""Paged display reads of a compression lineage, without deduping the whole transcript per page.

A long-lived chat is a lineage of compression segments (a Bot Chat: 6 to 25 of them, 2k to 15k rows,
5 to 40 MB of bodies). Display identity is not comparable across segments, so a page of one is "read
and dedupe every row of the lineage, then slice": each ``GET /api/sessions/<id>/messages?limit=120``
cost 0.6 to 2.8 s of CPU on a 14k-row lineage, on every chat switch and every tab reconcile.

Which rows are shown, and in which order, only moves when the lineage's rows do. That projection is
kept per (store file, lineage) as the ordered ids of the shown rows, under a stamp read in the same
read transaction as the rows it was built from:

- while the stamp holds, a page is the stamp query plus its own rows by id (a few ms);
- when the lineage only GREW (a turn appending rows nothing earlier can be a copy of), the new rows are
  deduped alone and appended to the kept order;
- anything else (a compaction, rewind, undo, delete, in-place rewrite, display-slot or uid backfill, or
  a new row that could be a copy of an earlier one) rebuilds the projection from the whole lineage.

Process-wide on purpose: the REST transcript routes open a fresh read-only handle per request.
"""

from __future__ import annotations

import os
import threading
from array import array
from bisect import bisect_left, insort
from collections import OrderedDict
from typing import Any, NamedTuple, Optional

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.conversation._store import messages as _messages
from persistence.sqlite.conversation._store.identity import _uid_list

_MAX_LINEAGES = 64
# A lineage that grew by more rows than this since it was last read is rebuilt, not extended.
_MAX_APPENDED_ROWS = 2000


class _Projection(NamedTuple):
    stamp: tuple
    order: array        # ids of the shown rows, in display order
    timestamps: array   # sorted: every row's timestamp (a display key carries it: copies share one)
    uids: array         # sorted: hash of every row's message uid and of each uid it absorbed


# (store file identity, lineage ids) -> _Projection
_projections: OrderedDict[tuple, _Projection] = OrderedDict()
_lock = threading.Lock()

_STAMP_COLUMNS = frozenset({"active", "compacted", "display_order", "message_uid", "absorbed_message_uids"})

# Everything the display projection reads, as index-only aggregates over the lineage:
#   [0]       the highest row id       an append (ids are AUTOINCREMENT: never reused);
#   [1] [2]   live rows: count, SUM(id)      a compaction, rewind, undo or delete (``transcript_cursor_state``),
#   [3] [4]   inactive rows: count, SUM(id)  and WHICH rows, not only how many;
#   [5] [6]   shown rows: count, SUM(id)     an archive flag flip;
#   [7] [8]   their display slots: count, SUM  a display-slot backfill or re-fold
#                                              (``later_tool_copy_ids`` reads ``display_order``);
#   [9]       live rows with a uid     a ``message_uid`` backfill;
#   [10]      SUM(transcript_revision) an in-place rewrite of any row's content or display columns, bumped
#                                      by the ``messages_*_revision_update`` triggers (``conversation_revision``).
_STAMP_SQL = (
    "SELECT top.id, live.n, live.ids, inactive.n, inactive.ids, shown.n, shown.ids, shown.slots, shown.slot_sum,"
    " uids.n, revision.n FROM"
    " (SELECT COALESCE(MAX(id), 0) AS id FROM messages WHERE session_id IN ({ids})) AS top,"
    " (SELECT COUNT(*) AS n, COALESCE(SUM(id), 0) AS ids FROM messages WHERE session_id IN ({ids})"
    " AND active = 1) AS live,"
    " (SELECT COUNT(*) AS n, COALESCE(SUM(id), 0) AS ids FROM messages WHERE session_id IN ({ids})"
    " AND active = 0) AS inactive,"
    " (SELECT COUNT(*) AS n, COALESCE(SUM(id), 0) AS ids, COUNT(display_order) AS slots,"
    " COALESCE(SUM(display_order), 0) AS slot_sum FROM messages WHERE session_id IN ({ids})"
    " AND (active = 1 OR compacted = 1)) AS shown,"
    " (SELECT COUNT(message_uid) AS n FROM messages WHERE session_id IN ({ids}) AND active = 1) AS uids,"
    " (SELECT COALESCE(SUM(transcript_revision), 0) AS n FROM sessions WHERE id IN ({ids})) AS revision")
_STAMP_PARTS = _STAMP_SQL.count("{ids}")
_TOP, _INACTIVE, _REVISION = 0, slice(3, 5), 10


def _marks(values) -> str:
    return ",".join("?" * len(values))


def _page(order, limit: int, offset: int, latest: bool):
    return order[::-1][offset:][:limit][::-1] if latest else order[offset:][:limit]


def _store_identity(holder: StoreHolder) -> Optional[tuple]:
    """The store FILE this holder reads: a recovered or recreated state.db at the same path is another."""
    path = getattr(holder, "db_path", None)
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return str(path), stat.st_dev, stat.st_ino


def _stampable(holder: StoreHolder, conn) -> bool:
    """Whether this store carries every column the stamp reads (a legacy read-only store may not)."""
    known = holder.__dict__.get("_lineage_page_stampable")
    if known is None:
        known = (_STAMP_COLUMNS <= set(_messages._message_column_names(holder, conn)) and any(
            row[1] == "transcript_revision" for row in conn.execute("PRAGMA table_info(sessions)")))
        holder.__dict__["_lineage_page_stampable"] = known
    return known


def _row_uids(row) -> list[int]:
    absorbed = row["absorbed_message_uids"]
    if absorbed:
        return [hash(uid) for uid in (row["message_uid"], *_uid_list(absorbed)) if uid]
    return [hash(row["message_uid"])] if row["message_uid"] else []


def _has(values: array, value) -> bool:
    at = bisect_left(values, value)
    return at < len(values) and values[at] == value


def _identities(rows) -> tuple[array, array]:
    """``(sorted timestamps, sorted uid hashes)`` of *rows*: what a later row must not share to be new."""
    timestamps = array("d", sorted({row["timestamp"] for row in rows if row["timestamp"] is not None}))
    uids = array("q", sorted({uid for row in rows for uid in _row_uids(row)}))
    return timestamps, uids


def _extended(holder: StoreHolder, conn, kept: _Projection, stamp: tuple, ids: tuple) -> Optional[_Projection]:
    """*kept* brought to *stamp* when the lineage only grew by rows no earlier row can be a copy of, else
    None (rebuild). Every display key carries its row's timestamp, and every other identity is a message
    uid: a new live row that shares neither with any earlier row has its own key and identities, sorts
    after every kept row (ordered by first row id), and neither hides nor revives one. So the projection
    of the grown lineage is the kept order followed by the new rows' own projection."""
    old = kept.stamp
    if stamp[_TOP] <= old[_TOP] or stamp[_INACTIVE] != old[_INACTIVE] or stamp[_REVISION] != old[_REVISION]:
        return None  # nothing appended, or a row went inactive, or one was rewritten in place
    new = conn.execute(f"SELECT * FROM messages WHERE session_id IN ({_marks(ids)}) AND id > ? ORDER BY id ASC",
                       (*ids, old[_TOP])).fetchall()
    if not new or len(new) > _MAX_APPENDED_ROWS or not all(row["active"] for row in new):
        return None
    count, id_sum = len(new), sum(row["id"] for row in new)
    slots = [row["display_order"] for row in new if row["display_order"] is not None]
    grown = (new[-1]["id"], old[1] + count, old[2] + id_sum, old[3], old[4], old[5] + count, old[6] + id_sum,
             old[7] + len(slots), old[8] + sum(slots),
             old[9] + sum(1 for row in new if row["message_uid"] is not None), old[10])
    if stamp != grown:
        return None  # an earlier row moved too (deleted, re-slotted, given a uid): not only growth
    for row in new:
        stamped = row["timestamp"]
        if stamped is None or _has(kept.timestamps, stamped) or any(_has(kept.uids, uid) for uid in _row_uids(row)):
            return None  # could be a copy of an earlier row: the full read decides
    # Copies AMONG the new rows are folded by their own projection below.
    timestamps, uids = array("d", kept.timestamps), array("q", kept.uids)
    for stamped in {row["timestamp"] for row in new}:
        insort(timestamps, stamped)
    for uid in {uid for row in new for uid in _row_uids(row)}:
        insort(uids, uid)
    order = array("q", kept.order)
    order.extend(row["id"] for row in _messages._dedupe_display_generations(holder, new, drop_removed=True))
    return _Projection(stamp, order, timestamps, uids)


def _publish(key: tuple, projection: _Projection) -> None:
    with _lock:
        _projections[key] = projection
        _projections.move_to_end(key)
        while len(_projections) > _MAX_LINEAGES:
            _projections.popitem(last=False)


def lineage_display_page(holder: StoreHolder, sql: str, session_ids: list[str], *, limit: int, offset: int,
                         latest: bool) -> Optional[list[Any]]:
    """One page of the lineage's deduped display rows (``messages.get_messages`` multi-segment semantics:
    *sql* is its full-lineage display read). None when this store cannot be stamped: the caller reads
    the whole lineage as before."""
    store = _store_identity(holder)
    if store is None:
        return None
    ids = tuple(session_ids)
    key = (store, ids)
    stamp_sql = _STAMP_SQL.format(ids=_marks(ids))

    def read(conn):
        if not _stampable(holder, conn):
            return None
        began = not conn.in_transaction  # inside a transcript_read_snapshot: its transaction
        if began:
            conn.execute("BEGIN")
        try:
            # The stamp and the rows below are one snapshot: the stamp names exactly these rows.
            stamp = tuple(conn.execute(stamp_sql, ids * _STAMP_PARTS).fetchone())
            with _lock:
                kept = _projections.get(key)
                if kept is not None:
                    _projections.move_to_end(key)
            grown = None
            if kept is not None and kept.stamp != stamp:
                kept = grown = _extended(holder, conn, kept, stamp, ids)
            if kept is not None:
                wanted = list(_page(kept.order, limit, offset, latest))
                found: dict[int, Any] = {}
                for start in range(0, len(wanted), 900):  # SQLite's bound-variable ceiling
                    chunk = wanted[start:start + 900]
                    found.update((row["id"], row) for row in conn.execute(
                        f"SELECT * FROM messages WHERE id IN ({_marks(chunk)})", chunk))
                if len(found) == len(wanted):
                    return stamp, grown, None, [found[row_id] for row_id in wanted]
            rows = _messages._with_removed_identities(holder, conn.execute(sql, ids).fetchall(), list(ids), conn)
            return stamp, None, rows, None
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")

    result = holder._read_retrying_ioerr(read)
    if result is None:
        return None
    stamp, grown, lineage_rows, page = result
    if page is not None:
        if grown is not None:
            _publish(key, grown)
        return page
    shown = _messages._dedupe_display_generations(holder, lineage_rows, drop_removed=True)
    _publish(key, _Projection(stamp, array("q", (row["id"] for row in shown)), *_identities(lineage_rows)))
    return _page(shown, limit, offset, latest)
