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
- when only the tip segment's ``transcript_revision`` moved (a live turn writing ``api_content``,
  pruning ``codex_reasoning_items`` on older assistant rows, or stamping non-visibility
  ``display_metadata`` on an assistant row), re-digesting the tip's rows with ``id <= old top`` over
  ``_DIGEST_COLUMNS`` proves whether every projection input on earlier rows is unchanged: if so, the
  kept projection is re-stamped (and extended with any appended rows above old top) instead of rebuilt.
  Exactness: page bodies are always re-read fresh by id; the projection (which ids, in what order)
  depends only on ``_DIGEST_COLUMNS`` of the lineage's rows; sessions whose revision did not move had
  no trigger-covered column rewritten (non-trigger columns stay guarded by the index aggregates in the
  stamp); the tip's rows up to old top are verified by digest; rows above old top are read fresh;
- anything else (a compaction, rewind, undo, delete, in-place rewrite of a projection input or a
  non-tip segment, display-slot or uid backfill, or a new row that could be a copy of an earlier one)
  rebuilds the projection from the whole lineage, single-flight per lineage and bounded process-wide.

Process-wide on purpose: the REST transcript routes open a fresh read-only handle per request.
"""

from __future__ import annotations

import hashlib
import os
import threading
from array import array
from bisect import bisect_left, insort
from collections import OrderedDict
from contextlib import closing
from typing import Any, NamedTuple, Optional

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.conversation._store import messages as _messages
from persistence.sqlite.conversation._store.identity import _uid_list

_MAX_LINEAGES = 64
# A lineage that grew by more rows than this since it was last read is rebuilt, not extended.
_MAX_APPENDED_ROWS = 2000
# A tip segment larger than this is rebuilt on a revision bump rather than digested row-by-row.
_MAX_VERIFY_ROWS = 4000
# Process-wide bound on concurrent full lineage rebuilds (prevents GIL storms across tabs/bots).
_MAX_CONCURRENT_REBUILDS = 2
_REBUILD_WAIT_TIMEOUT_S = 20.0

# Every messages column the display projection reads (except ``id``, which is always hashed first):
# _dedupe_display_generations (_display_dedupe_key, _stable_tool_key, _is_model_only_row,
# _fold_summary_carrier, later_tool_copy_ids, split_user_originated_turn) and _with_removed_identities.
_DIGEST_COLUMN_ORDER = (
    "session_id",
    "role",
    "content",
    "timestamp",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "display_kind",
    "display_metadata",
    "display_identity",
    "display_order",
    "message_uid",
    "absorbed_message_uids",
    "_compressed_summary",
    "active",
    "compacted",
)
_DIGEST_COLUMNS = frozenset(_DIGEST_COLUMN_ORDER)


class _Projection(NamedTuple):
    stamp: tuple
    order: array                  # ids of the shown rows, in display order
    timestamps: array             # sorted: every row's timestamp (a display key carries it: copies share one)
    uids: array                   # sorted: hash of every row's message uid and of each uid it absorbed
    tip: str                      # tip session id whose rows <= stamp[_TOP] were digested
    revisions: tuple[int, ...]    # per-session transcript_revision in lineage ids order
    digest_cols: tuple[str, ...]  # store-present subset of _DIGEST_COLUMN_ORDER hashed into digest
    digest: Optional[Any]         # running blake2b hash over tip rows <= stamp[_TOP] (never mutate)


# (store file identity, lineage ids) -> _Projection
_projections: OrderedDict[tuple, _Projection] = OrderedDict()
# (store file identity, lineage ids) -> Event set when the active full-rebuild flight finishes
_inflight: dict[tuple, threading.Event] = {}
_lock = threading.Lock()
_rebuild_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REBUILDS)


class _PageHit(NamedTuple):
    grown: Optional[_Projection]
    page: list[Any]


class _RebuildInput(NamedTuple):
    stamp: tuple
    revisions: tuple[int, ...]
    digest_cols: tuple[str, ...]
    digest: Optional[Any]
    rows: list[Any]


class _WaitFlight(NamedTuple):
    event: threading.Event


_NEED_SLOT = object()
_RETRY_READ = object()

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


def _digest_columns(holder: StoreHolder, conn) -> tuple[str, ...]:
    """Projection-input columns (except ``id``) present in this store, in deterministic order."""
    cols = holder.__dict__.get("_lineage_page_digest_columns")
    if cols is None:
        available = set(_messages._message_column_names(holder, conn))
        cols = tuple(col for col in _DIGEST_COLUMN_ORDER if col in available)
        holder.__dict__["_lineage_page_digest_columns"] = cols
    return cols


def _digest_update(hasher, rows, cols: tuple[str, ...], *, positional: bool = False) -> None:
    """Feed *rows* (ascending ``id``) into *hasher*.

    Non-user rows feed ``display_metadata`` only to ``_is_model_only_row`` (truthiness of
    ``MODEL_ONLY_DISPLAY_METADATA_KEY``); user rows also pass it into ``split_user_originated_turn``,
    so user rows digest the raw stored value while non-user rows digest ``bool(model_only)``.
    """
    role_idx = cols.index("role") + 1 if "role" in cols else -1
    meta_idx = cols.index("display_metadata") + 1 if "display_metadata" in cols else -1
    decode_meta = _messages._decode_display_metadata
    model_only_key = _messages.MODEL_ONLY_DISPLAY_METADATA_KEY
    for row in rows:
        values = list(row) if positional else [row["id"], *(row[col] for col in cols)]
        if meta_idx >= 0 and (role_idx < 0 or values[role_idx] != "user"):
            raw_meta = values[meta_idx]
            values[meta_idx] = bool(
                raw_meta is not None and (decode_meta(raw_meta) or {}).get(model_only_key)
            )
        hasher.update(repr(tuple(values)).encode("utf-8", "surrogatepass"))
        hasher.update(b"\n")


def _tip_digest(conn, tip: str, top_id: int, cols: tuple[str, ...]):
    """Running blake2b digest over all rows of *tip* with ``id <= top_id``, or None if too large."""
    count = conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE session_id = ? AND id <= ? LIMIT ?)",
        (tip, top_id, _MAX_VERIFY_ROWS + 1),
    ).fetchone()[0]
    if count > _MAX_VERIFY_ROWS:
        return None
    hasher = hashlib.blake2b(digest_size=16)
    select_cols = ", ".join(("id", *cols))
    with closing(conn.execute(
        f"SELECT {select_cols} FROM messages WHERE session_id = ? AND id <= ? ORDER BY id ASC",
        (tip, top_id),
    )) as cursor:
        _digest_update(hasher, cursor, cols, positional=True)
    return hasher


def _session_revisions(conn, ids: tuple[str, ...]) -> tuple[int, ...]:
    """Per-session ``transcript_revision`` in *ids* order."""
    by_id = {
        row[0]: int(row[1] or 0)
        for row in conn.execute(
            f"SELECT id, transcript_revision FROM sessions WHERE id IN ({_marks(ids)})",
            ids,
        )
    }
    return tuple(by_id.get(sid, 0) for sid in ids)


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


def _verify_tip_rewrite(
    holder: StoreHolder, conn, kept: _Projection, stamp: tuple, ids: tuple[str, ...]
) -> Optional[tuple[int, ...]]:
    """Verify a revision-moving change touched no projection input <= kept top; return new revisions or None."""
    if kept.digest is None or kept.tip != ids[-1]:
        return None
    cols = _digest_columns(holder, conn)
    if cols != kept.digest_cols:
        return None
    revisions = _session_revisions(conn, ids)
    if revisions[:-1] != kept.revisions[:-1]:
        return None
    current = _tip_digest(conn, kept.tip, kept.stamp[_TOP], cols)
    if current is None or current.digest() != kept.digest.digest():
        return None
    return revisions


def _extend_digest(holder: StoreHolder, conn, kept: _Projection, new) -> tuple[bool, Optional[Any]]:
    """Extend a copy of *kept*'s tip digest with newly appended tip rows; ``(False, None)`` on schema drift."""
    if kept.digest is None:
        return True, None
    cols = _digest_columns(holder, conn)
    if cols != kept.digest_cols:
        return False, None
    digest = kept.digest.copy()
    tip_new = [row for row in new if row["session_id"] == kept.tip]
    if tip_new:
        _digest_update(digest, tip_new, cols, positional=False)
    return True, digest


def _has_identity_collision(kept: _Projection, new) -> bool:
    for row in new:
        stamped = row["timestamp"]
        if stamped is None or _has(kept.timestamps, stamped) or any(_has(kept.uids, uid) for uid in _row_uids(row)):
            return True
    return False


def _extended(holder: StoreHolder, conn, kept: _Projection, stamp: tuple, ids: tuple) -> Optional[_Projection]:
    """*kept* brought to *stamp* when the lineage only grew (or only rewrote non-projection columns of the
    tip segment), else None (rebuild). Every display key carries its row's timestamp, and every other
    identity is a message uid: a new live row that shares neither with any earlier row has its own key and
    identities, sorts after every kept row (ordered by first row id), and neither hides nor revives one.
    So the projection of the grown lineage is the kept order followed by the new rows' own projection."""
    old = kept.stamp
    if stamp[_TOP] < old[_TOP] or stamp[_INACTIVE] != old[_INACTIVE]:
        return None  # shrank/deleted top, or a row went inactive
    revisions: Optional[tuple[int, ...]] = None
    if stamp[_REVISION] != old[_REVISION]:
        revisions = _verify_tip_rewrite(holder, conn, kept, stamp, ids)
        if revisions is None:
            return None
    if stamp[_TOP] == old[_TOP]:
        # Nothing appended: valid only when every non-revision stamp part holds and the tip digest matched.
        if revisions is None or stamp[:_REVISION] != old[:_REVISION]:
            return None
        return _Projection(
            stamp, kept.order, kept.timestamps, kept.uids, kept.tip, revisions, kept.digest_cols, kept.digest
        )
    new = conn.execute(
        f"SELECT * FROM messages WHERE session_id IN ({_marks(ids)}) AND id > ? ORDER BY id ASC",
        (*ids, old[_TOP]),
    ).fetchall()
    if not new or len(new) > _MAX_APPENDED_ROWS or not all(row["active"] for row in new):
        return None
    count, id_sum = len(new), sum(row["id"] for row in new)
    slots = [row["display_order"] for row in new if row["display_order"] is not None]
    grown = (
        new[-1]["id"], old[1] + count, old[2] + id_sum, old[3], old[4], old[5] + count, old[6] + id_sum,
        old[7] + len(slots), old[8] + sum(slots),
        old[9] + sum(1 for row in new if row["message_uid"] is not None), stamp[_REVISION],
    )
    if stamp != grown or _has_identity_collision(kept, new):
        return None  # earlier row moved (deleted, re-slotted, given a uid) or new row may copy an earlier one
    if revisions is None:
        revisions = _session_revisions(conn, ids)
        if revisions != kept.revisions:
            return None
    ok, digest = _extend_digest(holder, conn, kept, new)
    if not ok:
        return None
    # Copies AMONG the new rows are folded by their own projection below.
    timestamps, uids = array("d", kept.timestamps), array("q", kept.uids)
    for stamped in {row["timestamp"] for row in new}:
        insort(timestamps, stamped)
    for uid in {uid for row in new for uid in _row_uids(row)}:
        insort(uids, uid)
    order = array("q", kept.order)
    order.extend(row["id"] for row in _messages._dedupe_display_generations(holder, new, drop_removed=True))
    return _Projection(stamp, order, timestamps, uids, kept.tip, revisions, kept.digest_cols, digest)


def _publish(key: tuple, projection: _Projection) -> None:
    with _lock:
        _projections[key] = projection
        _projections.move_to_end(key)
        while len(_projections) > _MAX_LINEAGES:
            _projections.popitem(last=False)


def _fetch_page_rows(conn, wanted: list[int]) -> Optional[list[Any]]:
    found: dict[int, Any] = {}
    for start in range(0, len(wanted), 900):  # SQLite's bound-variable ceiling
        chunk = wanted[start:start + 900]
        found.update((row["id"], row) for row in conn.execute(
            f"SELECT * FROM messages WHERE id IN ({_marks(chunk)})", chunk))
    if len(found) != len(wanted):
        return None
    return [found[row_id] for row_id in wanted]


def _try_fast_page(
    holder: StoreHolder, conn, key: tuple, stamp: tuple, ids: tuple[str, ...],
    *, limit: int, offset: int, latest: bool,
) -> tuple[Optional[_Projection], Optional[_Projection], Optional[list[Any]]]:
    """Return ``(initial_kept, grown, page)``; *page* is None when a full rebuild is needed."""
    with _lock:
        kept = _projections.get(key)
        if kept is not None:
            _projections.move_to_end(key)
    initial_kept = kept
    grown = None
    if kept is not None and kept.stamp != stamp:
        kept = grown = _extended(holder, conn, kept, stamp, ids)
    if kept is None:
        return initial_kept, None, None
    return initial_kept, grown, _fetch_page_rows(conn, list(_page(kept.order, limit, offset, latest)))


def _claim_or_wait_flight(
    key: tuple, initial_kept: Optional[_Projection], bypass_flight: bool
) -> tuple[Optional[threading.Event], Any]:
    """Return ``(own_flight, None)`` when this caller becomes the builder, else ``(None, action)``."""
    with _lock:
        fresh = _projections.get(key)
        if fresh is not None and fresh is not initial_kept:
            return None, _RETRY_READ
        existing = _inflight.get(key)
        if existing is not None and not bypass_flight:
            return None, _WaitFlight(existing)
        flight = threading.Event()
        _inflight[key] = flight
        return flight, None


def _release_flight(key: tuple, flight: Optional[threading.Event]) -> None:
    if flight is None:
        return
    with _lock:
        if _inflight.get(key) is flight:
            del _inflight[key]
    flight.set()


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

    own_flight: Optional[threading.Event] = None
    has_slot = False
    bypass_flight = False

    def read(conn):
        nonlocal own_flight, has_slot
        if not _stampable(holder, conn):
            return None
        began = not conn.in_transaction  # inside a transcript_read_snapshot: its transaction
        if began:
            conn.execute("BEGIN")
        try:
            # The stamp and the rows below are one snapshot: the stamp names exactly these rows.
            stamp = tuple(conn.execute(stamp_sql, ids * _STAMP_PARTS).fetchone())
            initial_kept, grown, page = _try_fast_page(
                holder, conn, key, stamp, ids, limit=limit, offset=offset, latest=latest,
            )
            if page is not None:
                return _PageHit(grown, page)
            # Register our flight BEFORE waiting for a rebuild slot so same-key callers queue on us.
            if own_flight is None:
                flight, wait_action = _claim_or_wait_flight(key, initial_kept, bypass_flight)
                if flight is None:
                    return wait_action
                own_flight = flight
            if not has_slot:
                has_slot = _rebuild_slots.acquire(blocking=False)
                if not has_slot:
                    return _NEED_SLOT
            revisions = _session_revisions(conn, ids)
            digest_cols = _digest_columns(holder, conn)
            digest = _tip_digest(conn, ids[-1], stamp[_TOP], digest_cols)
            rows = _messages._with_removed_identities(holder, conn.execute(sql, ids).fetchall(), list(ids), conn)
            return _RebuildInput(stamp, revisions, digest_cols, digest, rows)
        finally:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")

    try:
        while True:
            result = holder._read_retrying_ioerr(read)
            if result is None:
                return None
            if isinstance(result, _PageHit):
                if result.grown is not None:
                    _publish(key, result.grown)
                return result.page
            if isinstance(result, _RebuildInput):
                shown = _messages._dedupe_display_generations(holder, result.rows, drop_removed=True)
                _publish(
                    key,
                    _Projection(
                        result.stamp, array("q", (row["id"] for row in shown)), *_identities(result.rows),
                        ids[-1], result.revisions, result.digest_cols, result.digest,
                    ),
                )
                return _page(shown, limit, offset, latest)
            if isinstance(result, _WaitFlight):
                if has_slot:
                    has_slot = False
                    _rebuild_slots.release()
                if not result.event.wait(_REBUILD_WAIT_TIMEOUT_S):
                    bypass_flight = True
            elif result is _NEED_SLOT:
                has_slot = _rebuild_slots.acquire()
    finally:
        if has_slot:
            has_slot = False
            _rebuild_slots.release()
        if own_flight is not None:
            _release_flight(key, own_flight)
            own_flight = None
