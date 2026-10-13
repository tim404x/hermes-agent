"""Side-window marker mixin for SessionDB: the stamp ``message_agent`` puts on a busy teammate's
side window (``tools/bot_mode_dm_side_window.py``), and the listing the desktop reads to open it
as a tab.

The stamp lives in ``sessions.origin_json`` as ``{"side_window": {"at": <epoch>, "by": <author id>}}``.
``origin_json`` is the session's provenance record (gateway routing peer, import origin); a CLI
side window has none, and compression continuations inherit the column, so the mark follows the
live tip without a schema change. A row that already carries another origin is never touched.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from hermes_state_common import _sql_json_extract

SIDE_WINDOW_KEY = "side_window"
_AT_SQL = _sql_json_extract("origin_json", f"$.{SIDE_WINDOW_KEY}.at")


def _side_window_origin(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """The stamp inside an ``origin_json`` value, or None when it carries none."""
    try:
        origin = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None
    stamp = origin.get(SIDE_WINDOW_KEY) if isinstance(origin, dict) else None
    return stamp if isinstance(stamp, dict) else None


class SessionSideWindowsMixin:
    """Stamp and list ``message_agent`` side windows."""

    def mark_side_window(self, session_id: str, *, by: str | None = None, at: float | None = None) -> bool:
        """Stamp ``session_id`` as a side window that just received a DM (``at`` defaults to now).
        False when the row is missing or carries a non-side-window origin, which is never overwritten."""
        payload = json.dumps({SIDE_WINDOW_KEY: {"at": float(time.time() if at is None else at), "by": by or ""}})

        def _do(conn) -> bool:
            row = conn.execute("SELECT origin_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return False
            raw = row[0]
            try:
                origin = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                return False
            # Only an empty origin or an earlier stamp of our own: a gateway routing peer or an
            # import record is load-bearing for other readers.
            if origin is not None and not (isinstance(origin, dict) and set(origin) == {SIDE_WINDOW_KEY}):
                return False
            conn.execute("UPDATE sessions SET origin_json = ? WHERE id = ?", (payload, session_id))
            return True

        return bool(self._execute_write(_do))

    def list_side_windows(self, limit: int = 8) -> list[dict[str, Any]]:
        """Stamped side windows, newest stamp first, one entry per compression lineage keyed by its
        live tip: ``{id, title, at, by, last_active, message_count}``. Archived rows are left out."""
        rows = self._read_all(
            "SELECT id, title, origin_json FROM sessions"
            f" WHERE {_AT_SQL} IS NOT NULL AND COALESCE(archived, 0) = 0"
            f" ORDER BY {_AT_SQL} DESC LIMIT ?",
            # A lineage's root and tip both carry the stamp, so over-read before folding.
            (max(1, int(limit)) * 4,))
        windows: dict[str, dict[str, Any]] = {}
        for row in rows:
            stamp = _side_window_origin(row["origin_json"]) or {}
            tip = self.get_compression_tip(row["id"]) or row["id"]
            if tip in windows:
                continue
            tip_row = self.get_session(tip) or {}
            windows[tip] = {
                "id": tip,
                "title": tip_row.get("title") or row["title"] or "",
                "at": float(stamp.get("at") or 0),
                "by": str(stamp.get("by") or ""),
                "last_active": tip_row.get("last_activity_at") or tip_row.get("started_at") or 0,
                "message_count": tip_row.get("message_count") or 0,
            }
        return sorted(windows.values(), key=lambda w: w["at"], reverse=True)[:max(1, int(limit))]
