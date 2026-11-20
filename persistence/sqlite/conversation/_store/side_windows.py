"""Fork carry (bot mode): the side-window stamp ``message_agent`` puts on a teammate's titled side window
(``tools/bot_mode_dm_side_window.py``), and the listings the desktop and the next DM read it back through.

The stamp lives in its own ``sessions.side_window_json`` cell as ``{"at": <epoch>, "by": <author id>, "key":
<conversation key>}``, so any row can carry it, a gateway-created chat (whose ``origin_json`` holds its routing peer)
included; ``origin_json`` is never written here. A compression continuation inherits the cell. Rows stamped before
the cell existed (or by hand) carry the stamp inside ``origin_json`` under ``SIDE_WINDOW_KEY``
(``persistence.conversation.sessions``); every reader falls back to that legacy stamp when the cell is empty.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from persistence.conversation.sessions import SIDE_WINDOW_KEY, side_window_stamp
from persistence.sqlite._holder import StoreHolder
from persistence.sqlite._schema_ddl import _sql_json_extract
from persistence.sqlite.conversation._store.compression import get_compression_lineage, get_compression_tip
from persistence.sqlite.conversation._store.messages import get_messages
from persistence.sqlite.conversation._store.sessions import get_session


def _stamp_sql(field: str) -> str:
    """The stamp's ``field``: the own cell's, else the legacy ``origin_json`` stamp's."""
    return (f"COALESCE({_sql_json_extract('side_window_json', f'$.{field}')},"
            f" {_sql_json_extract('origin_json', f'$.{SIDE_WINDOW_KEY}.{field}')})")


_AT_SQL = _stamp_sql("at")
_KEY_SQL = _stamp_sql("key")


def _row_stamp(row: Any) -> dict[str, Any]:
    """The stamp of a ``(side_window_json, origin_json)`` row: the own cell, else the legacy origin stamp."""
    try:
        own = json.loads(row["side_window_json"]) if row["side_window_json"] else None
    except (TypeError, ValueError):
        own = None
    if isinstance(own, dict):
        return own
    return side_window_stamp(row["origin_json"]) or {}


def mark_side_window(holder: StoreHolder, session_id: str, *, by: Optional[str] = None, at: Optional[float] = None,
                     key: Optional[str] = None) -> bool:
    """Stamp ``session_id`` as a side window that just received a DM (``at`` defaults to now). ``key`` names the
    conversation; a stamp without one keeps the key already there (own cell or legacy origin stamp). Writes only
    ``side_window_json``, never ``origin_json``. False when the row is missing."""
    stamp: dict[str, Any] = {"at": float(time.time() if at is None else at), "by": by or ""}

    def _do(conn) -> bool:
        row = conn.execute("SELECT side_window_json, origin_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return False
        kept = _row_stamp(row).get("key")
        if key or kept:
            stamp["key"] = key or kept
        conn.execute("UPDATE sessions SET side_window_json = ? WHERE id = ?", (json.dumps(stamp), session_id))
        return True

    return bool(holder._execute_write(_do))


def _stamped_rows(holder: StoreHolder, where: str, params: tuple, limit: int) -> list:
    # A lineage's root and tip both carry the stamp, so over-read before folding.
    return holder._read_all(
        "SELECT id, title, side_window_json, origin_json FROM sessions"
        f" WHERE {where} AND COALESCE(archived, 0) = 0"
        f" ORDER BY {_AT_SQL} DESC LIMIT ?",
        (*params, max(1, int(limit)) * 4))


def list_side_windows(holder: StoreHolder, limit: int = 8) -> list[dict[str, Any]]:
    """Stamped side windows, newest stamp first, one entry per compression lineage keyed by its live tip:
    ``{id, title, at, by, last_active, message_count}``. Archived rows are left out."""
    windows: dict[str, dict[str, Any]] = {}
    for row in _stamped_rows(holder, f"{_AT_SQL} IS NOT NULL", (), limit):
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
    for row in _stamped_rows(holder, f"{_KEY_SQL} = ?", (key,), limit):
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
