"""Fork carry (bot mode): the side-window stamp ``message_agent`` puts on a teammate's titled side window
(``tools/bot_mode_dm_side_window.py``), and the listings the desktop and the next DM read it back through.

The stamp lives in ``sessions.origin_json`` as ``{"side_window": {"at": <epoch>, "by": <author id>, "key":
<conversation key>}}`` (``persistence.conversation.sessions.SIDE_WINDOW_KEY``). ``origin_json`` is the session's
provenance record (gateway routing peer, import origin); a CLI side window has none, and a compression continuation
inherits its parent's, so the mark follows the live tip without a schema change. A row that already carries another
origin is never touched.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from persistence.conversation.sessions import SIDE_WINDOW_KEY, origin_is_side_window_only, side_window_stamp
from persistence.sqlite._holder import StoreHolder
from persistence.sqlite._schema_ddl import _sql_json_extract
from persistence.sqlite.conversation._store.compression import get_compression_lineage, get_compression_tip
from persistence.sqlite.conversation._store.messages import get_messages
from persistence.sqlite.conversation._store.sessions import get_session

_AT_SQL = _sql_json_extract("origin_json", f"$.{SIDE_WINDOW_KEY}.at")
_KEY_SQL = _sql_json_extract("origin_json", f"$.{SIDE_WINDOW_KEY}.key")


def mark_side_window(holder: StoreHolder, session_id: str, *, by: Optional[str] = None, at: Optional[float] = None,
                     key: Optional[str] = None) -> bool:
    """Stamp ``session_id`` as a side window that just received a DM (``at`` defaults to now). ``key`` names the
    conversation; a stamp without one keeps the key already there. False when the row is missing or carries a
    non-side-window origin, which is never overwritten."""
    stamp: dict[str, Any] = {"at": float(time.time() if at is None else at), "by": by or ""}

    def _do(conn) -> bool:
        row = conn.execute("SELECT origin_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return False
        try:
            origin = json.loads(row[0]) if row[0] else None
        except (TypeError, ValueError):
            return False
        # Only an empty origin or an earlier stamp of our own: a gateway routing peer or an import record is
        # load-bearing for other readers.
        if origin is not None and not origin_is_side_window_only(origin):
            return False
        kept = (side_window_stamp(origin) or {}).get("key")
        if key or kept:
            stamp["key"] = key or kept
        conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?",
                     (json.dumps({SIDE_WINDOW_KEY: stamp}), session_id))
        return True

    return bool(holder._execute_write(_do))


def _stamped_rows(holder: StoreHolder, where: str, params: tuple, limit: int) -> list:
    # A lineage's root and tip both carry the stamp, so over-read before folding.
    return holder._read_all(
        "SELECT id, title, origin_json FROM sessions"
        f" WHERE {where} AND COALESCE(archived, 0) = 0"
        f" ORDER BY {_AT_SQL} DESC LIMIT ?",
        (*params, max(1, int(limit)) * 4))


def list_side_windows(holder: StoreHolder, limit: int = 8) -> list[dict[str, Any]]:
    """Stamped side windows, newest stamp first, one entry per compression lineage keyed by its live tip:
    ``{id, title, at, by, last_active, message_count}``. Archived rows are left out."""
    windows: dict[str, dict[str, Any]] = {}
    for row in _stamped_rows(holder, f"{_AT_SQL} IS NOT NULL", (), limit):
        stamp = side_window_stamp(row["origin_json"]) or {}
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
        at = float((side_window_stamp(row["origin_json"]) or {}).get("at") or 0)
        marks = (at, tip_row.get("last_activity_at"), tip_row.get("started_at"),
                 newest[-1].get("timestamp") if newest else None)
        windows[tip] = {
            "id": tip,
            "title": root_row.get("title") or tip_row.get("title") or "",
            "at": at,
            "last_active": max(float(m or 0) for m in marks),
        }
    return sorted(windows.values(), key=lambda w: w["at"], reverse=True)[:max(1, int(limit))]
