"""Fork carry (bot mode): the side-window stamp ``message_agent`` puts on a teammate's titled side window
(``tools/bot_mode_dm_side_window.py``), and the listings the desktop and the next DM read it back through.

The stamp lives in its own ``sessions.side_window_json`` cell as ``{"at": <epoch>, "by": <author id>, "key":
<conversation key>, "dm": true}`` (``key`` and ``dm`` only when set), so any row can carry it, a gateway-created chat
(whose ``origin_json`` holds its routing peer) included. A compression continuation inherits the cell. Rows stamped
before the cell existed (or by hand) carry the stamp inside ``origin_json`` under ``SIDE_WINDOW_KEY``
(``persistence.conversation.sessions``); every reader falls back to that legacy stamp when the cell is empty.

Rollback safety: a write mirrors the stamp into ``origin_json`` as ``{"side_window": stamp}`` when that origin is
empty or already exactly a side-window stamp (``origin_is_side_window_only``), so a build that predates the cell
still sees the tab. A gateway routing origin is never touched.

Schema tolerance: the cell arrived with schema event 35. A store still on the previous schema (another profile's
``state.db`` opened only through ``open_persistence_reader``, which never migrates) has no such column, so every
statement here feature-detects it per store (:func:`has_side_window_cell`) and falls back to the legacy origin
stamp alone, reading ``NULL AS side_window_json``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import weakref
from typing import Any, Optional

from persistence.conversation.sessions import (
    CANONICAL_BOT_CHAT_TITLE, SIDE_WINDOW_KEY, origin_is_side_window_only, side_window_stamp,
)
from persistence.sqlite._holder import StoreHolder
from persistence.sqlite._schema_ddl import _sql_json_extract
from persistence.sqlite.conversation._store.compression import get_compression_lineage, get_compression_tip
from persistence.sqlite.conversation._store.messages import get_messages
from persistence.sqlite.conversation._store.sessions import get_session

logger = logging.getLogger("hermes_state")

_CELL = "side_window_json"

# Stores known to have the cell. Only a hit is cached: a column never disappears from a live store, while a
# reader on an unmigrated store must see the cell as soon as that profile's writer migrates it.
_CELL_PRESENT: "weakref.WeakKeyDictionary[Any, bool]" = weakref.WeakKeyDictionary()


def _probe_cell(conn) -> bool:
    return any(row[1] == _CELL for row in conn.execute("PRAGMA table_info(sessions)"))


def has_side_window_cell(holder: StoreHolder, conn: Any = None) -> bool:
    """Whether this store's ``sessions`` table has the ``side_window_json`` cell (schema event 35). Probes ``conn``
    when given (the connection the caller is about to run its statement on), else a read connection."""
    try:
        if _CELL_PRESENT.get(holder):
            return True
    except TypeError:  # an unhashable or non-weakrefable holder: probe every time
        pass
    present = _probe_cell(conn) if conn is not None else bool(holder._read_retrying_ioerr(_probe_cell))
    if present:
        try:
            _CELL_PRESENT[holder] = True
        except TypeError:
            pass
    return present


def _cell_select(cell: bool) -> str:
    """The SELECT term for the cell: the column, or ``NULL AS side_window_json`` on the previous schema."""
    return _CELL if cell else f"NULL AS {_CELL}"


def compact_session_cols(holder: StoreHolder, cols_sql: str) -> str:
    """A compact-listing ``s.``-prefixed SELECT list made safe for this store: on the previous schema
    ``s.side_window_json`` reads as ``NULL AS side_window_json``."""
    if has_side_window_cell(holder):
        return cols_sql
    return ", ".join(f"NULL AS {_CELL}" if col.strip() == f"s.{_CELL}" else col for col in cols_sql.split(","))


def _stamp_sql(field: str, cell: bool = True) -> str:
    """The stamp's ``field``: the own cell's, else the legacy ``origin_json`` stamp's (the legacy one alone on the
    previous schema)."""
    legacy = _sql_json_extract("origin_json", f"$.{SIDE_WINDOW_KEY}.{field}")
    if not cell:
        return legacy
    return f"COALESCE({_sql_json_extract(_CELL, f'$.{field}')}, {legacy})"


def _row_stamp(row: Any) -> dict[str, Any]:
    """The stamp of a ``(side_window_json, origin_json)`` row: the own cell, else the legacy origin stamp."""
    try:
        own = json.loads(row[_CELL]) if row[_CELL] else None
    except (TypeError, ValueError):
        own = None
    if isinstance(own, dict):
        return own
    return side_window_stamp(row["origin_json"]) or {}


def _is_bot_chat(title: Any) -> bool:
    """Whether ``title`` names the canonical Bot Chat (whitespace runs and case ignored)."""
    return " ".join(str(title or "").split()).casefold() == CANONICAL_BOT_CHAT_TITLE.casefold()


def _origin_writable(origin_json: Any) -> bool:
    """Whether a side-window stamp may be written into this ``origin_json``: empty, or exactly an earlier stamp."""
    if not origin_json:
        return True
    try:
        if json.loads(origin_json) in ({}, None):
            return True
    except (TypeError, ValueError):
        return False
    return origin_is_side_window_only(origin_json)


def mark_side_window(holder: StoreHolder, session_id: str, *, by: Optional[str] = None, at: Optional[float] = None,
                     key: Optional[str] = None, dm: bool = False) -> bool:
    """Stamp ``session_id`` as a side window that just received a DM (``at`` defaults to now). ``key`` names the
    conversation and ``dm`` marks a direct-message window; a stamp without them keeps the ones already there (own
    cell or legacy origin stamp). Writes ``side_window_json`` and mirrors the stamp into an empty or stamp-only
    ``origin_json``. On the previous schema (no cell) only that origin stamp is written. False, with nothing
    written, when the row is missing, the row or its compression-lineage root is the canonical Bot Chat, or (no
    cell) its origin is a routing peer."""
    lineage = get_compression_lineage(holder, session_id)
    if not lineage:
        return False
    if lineage[0] != session_id and _is_bot_chat((get_session(holder, lineage[0]) or {}).get("title")):
        return False
    stamp: dict[str, Any] = {"at": float(time.time() if at is None else at), "by": by or ""}

    def _do(conn) -> bool:
        cell = has_side_window_cell(holder, conn)
        row = conn.execute(f"SELECT title, {_cell_select(cell)}, origin_json FROM sessions WHERE id = ?",
                           (session_id,)).fetchone()
        if row is None or _is_bot_chat(row["title"]):
            return False
        prior = _row_stamp(row)
        if key or prior.get("key"):
            stamp["key"] = key or prior.get("key")
        if dm or prior.get("dm") is True:
            stamp["dm"] = True
        text, origin_ok = json.dumps(stamp), _origin_writable(row["origin_json"])
        mirror = json.dumps({SIDE_WINDOW_KEY: stamp})
        if not cell:
            if not origin_ok:
                return False
            conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?", (mirror, session_id))
            return True
        conn.execute(f"UPDATE sessions SET {_CELL} = ? WHERE id = ?", (text, session_id))
        if origin_ok:
            try:
                conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?", (mirror, session_id))
            except sqlite3.Error:  # the mirror is a courtesy to older builds; the cell is the stamp
                logger.debug("side-window origin mirror failed for %s", session_id, exc_info=True)
        return True

    return bool(holder._execute_write(_do))


def _stamped_rows(holder: StoreHolder, field: str, test_sql: str, params: tuple, limit: int) -> list:
    """Unarchived rows whose stamp ``field`` passes ``test_sql``, newest stamp first. A lineage's root and tip both
    carry the stamp, so over-read before folding."""
    def _read(conn) -> list:
        cell = has_side_window_cell(holder, conn)
        return conn.execute(
            f"SELECT id, title, {_cell_select(cell)}, origin_json FROM sessions"
            f" WHERE {_stamp_sql(field, cell)} {test_sql} AND COALESCE(archived, 0) = 0"
            f" ORDER BY {_stamp_sql('at', cell)} DESC LIMIT ?",
            (*params, max(1, int(limit)) * 4)).fetchall()

    return holder._read_retrying_ioerr(_read)


def list_side_windows(holder: StoreHolder, limit: int = 8) -> list[dict[str, Any]]:
    """Stamped side windows, newest stamp first, one entry per compression lineage keyed by its live tip:
    ``{id, title, at, by, last_active, message_count}``. Archived rows are left out."""
    windows: dict[str, dict[str, Any]] = {}
    for row in _stamped_rows(holder, "at", "IS NOT NULL", (), limit):
        stamp = _row_stamp(row)
        tip = get_compression_tip(holder, row["id"]) or row["id"]
        if tip in windows:
            continue
        tip_row = get_session(holder, tip) or {}
        windows[tip] = {
            "id": tip,
            "title": tip_row.get("title") or row["title"] or "",
            "at": float(stamp.get("at") or 0),
            "by": str(stamp.get("by") or ""),
            "last_active": float(tip_row.get("last_activity_at") or tip_row.get("started_at") or 0),
            "message_count": int(tip_row.get("message_count") or 0),
        }
    return sorted(windows.values(), key=lambda w: w["at"], reverse=True)[:max(1, int(limit))]


def find_side_windows(holder: StoreHolder, key: str, limit: int = 8) -> list[dict[str, Any]]:
    """The side windows stamped with conversation ``key``, newest stamp first, one entry per compression lineage:
    ``{id, title, at, last_active}``. ``id`` is the live tip; ``title`` is the lineage root's, the name a
    ``chat -c <title>`` transport resolves to that tip. ``last_active`` is the freshest of the stamp, the tip's
    start and activity marks and its newest message. Archived rows are left out."""
    windows: dict[str, dict[str, Any]] = {}
    for row in _stamped_rows(holder, "key", "= ?", (key,), limit):
        tip = get_compression_tip(holder, row["id"]) or row["id"]
        if tip in windows:
            continue
        root = (get_compression_lineage(holder, tip) or [tip])[0]
        tip_row, root_row = get_session(holder, tip) or {}, get_session(holder, root) or {}
        newest = get_messages(holder, tip, latest=True, limit=1)
        at = float(_row_stamp(row).get("at") or 0)
        marks = (at, tip_row.get("last_activity_at"), tip_row.get("started_at"),
                 newest[-1].get("timestamp") if newest else None)
        windows[tip] = {
            "id": tip,
            "title": root_row.get("title") or tip_row.get("title") or "",
            "at": at,
            "last_active": max(float(m or 0) for m in marks),
        }
    return sorted(windows.values(), key=lambda w: w["at"], reverse=True)[:max(1, int(limit))]
