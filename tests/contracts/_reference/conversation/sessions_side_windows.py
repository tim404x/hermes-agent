"""Fork carry (bot mode): the side-window stamp of the in-memory session reference, mixed into
``InMemorySessionRepository`` (``sessions.py``), which supplies the row storage and the record builder.

The stamp is the row's own ``side_window_json`` cell, as the SQLite store keeps it
(``persistence/sqlite/conversation/_store/side_windows.py``), never its ``origin``; a legacy stamp under
``origin[SIDE_WINDOW_KEY]`` still reads. Semantics follow ``persistence/conversation/sessions.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from persistence import SessionId
from persistence.conversation.messages import TranscriptRead
from persistence.conversation.sessions import SideWindow, SideWindowMatch, side_window_stamp


def _row_stamp(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The row's own stamp cell, else the legacy stamp inside its origin."""
    if row.get("side_window_json"):
        return dict(json.loads(row["side_window_json"]))
    return side_window_stamp(row["origin"])


class SessionSideWindowsMixin:
    """``mark_side_window``, ``list_side_windows`` and ``find_side_windows``."""

    def mark_side_window(self, session_id: SessionId, *, by: Optional[str] = None, at: Optional[float] = None,
                         key: Optional[str] = None) -> bool:
        scope = self._backend.scope
        stamp: dict[str, Any] = {"at": float(time.time() if at is None else at), "by": by or ""}
        with self._flags_lock:
            row = self._table(scope).get(session_id)
            if row is None:
                return False
            kept = (_row_stamp(row) or {}).get("key")
            if key or kept:
                stamp["key"] = key or kept
            row["side_window_json"] = json.dumps(stamp)
        return True

    def _stamped(self, *, key: Optional[str], limit: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """``(row, stamp)`` of the unarchived stamped rows (with ``key``: stamped with it), newest stamp first, over-read
        to ``limit * 4`` as the store does before folding by tip."""
        scope = self._backend.scope
        stamped = []
        for row in self._table(scope).values():
            stamp = _row_stamp(row)
            if stamp is None or stamp.get("at") is None or row["archived"]:
                continue
            if key is not None and stamp.get("key") != key:
                continue
            stamped.append((row, stamp))
        stamped.sort(key=lambda pair: float(pair[1].get("at") or 0), reverse=True)
        return stamped[:max(1, int(limit)) * 4]

    def list_side_windows(self, limit: int = 8) -> tuple[SideWindow, ...]:
        scope = self._backend.scope
        table = self._table(scope)
        windows: dict[str, SideWindow] = {}
        for row, stamp in self._stamped(key=None, limit=limit):
            tip = str(self._backend.lineage.tip(SessionId(row["session_id"])))
            if tip in windows:
                continue
            tip_record = self._record(scope, table[tip]) if tip in table else None
            windows[tip] = SideWindow(
                id=SessionId(tip),
                title=(tip_record.title if tip_record else None) or row["title"] or "",
                at=float(stamp.get("at") or 0),
                by=str(stamp.get("by") or ""),
                last_active=float((tip_record.last_activity_at or tip_record.started_at or 0) if tip_record else 0),
                message_count=int(tip_record.message_count or 0) if tip_record else 0,
            )
        return tuple(sorted(windows.values(), key=lambda w: w.at, reverse=True)[:max(1, int(limit))])

    def find_side_windows(self, key: str, limit: int = 8) -> tuple[SideWindowMatch, ...]:
        scope = self._backend.scope
        table = self._table(scope)
        windows: dict[str, SideWindowMatch] = {}
        for row, stamp in self._stamped(key=key, limit=limit):
            tip = str(self._backend.lineage.tip(SessionId(row["session_id"])))
            if tip in windows:
                continue
            root = str((self._backend.lineage.lineage(SessionId(tip)) or (tip,))[0])
            tip_record = self._record(scope, table[tip]) if tip in table else None
            root_record = self._record(scope, table[root]) if root in table else None
            newest = self._backend.messages.read_transcript(SessionId(tip), TranscriptRead(latest=True, limit=1))
            at = float(stamp.get("at") or 0)
            marks = (at, tip_record.last_activity_at if tip_record else None,
                     tip_record.started_at if tip_record else None, newest[-1].timestamp if newest else None)
            windows[tip] = SideWindowMatch(
                id=SessionId(tip),
                title=(root_record.title if root_record else None) or (tip_record.title if tip_record else None) or "",
                at=at,
                last_active=max(float(m or 0) for m in marks),
            )
        return tuple(sorted(windows.values(), key=lambda w: w.at, reverse=True)[:max(1, int(limit))])
