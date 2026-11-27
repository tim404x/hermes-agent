"""Dispatch resume deadlines, parked-row selection, and dangling attempt recovery."""

import re
import sqlite3
from datetime import UTC, datetime
from typing import Optional


def _scheduled_iso_epoch(text: str) -> int:
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("scheduled time must include Z or a UTC offset")
    return int(parsed.astimezone(UTC).replace(microsecond=0).timestamp())


def coerce_until(value: object) -> Optional[int]:
    """Parse an explicit deadline; reject ambiguous local times and unrepresentable epochs."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value.isdecimal():
                return _scheduled_iso_epoch(value)
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        epoch = int(value)
        datetime.fromtimestamp(epoch, UTC)
        return epoch
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("until must be epoch seconds or an ISO8601 timestamp with Z or a UTC offset") from exc


def parse_scheduled_until(text: Optional[str]) -> Optional[int]:
    """Read an optional ISO8601 wake-up marker from free-form prose; ignore malformed markers."""
    marker = re.search(r"SCHEDULED_UNTIL=(\S+)", text) if isinstance(text, str) else None
    if not marker:
        return None
    try:
        return _scheduled_iso_epoch(marker[1].rstrip(".,;)"))
    except (ValueError, OverflowError, OSError):
        return None


def format_scheduled_until(epoch: int) -> str:
    """Canonical UTC deadline for CLI and tool responses, independent of the host timezone."""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def parked_status(conn: sqlite3.Connection, task_id: str, auto_release: Optional[dict]) -> Optional[str]:
    """Under the unblock write lock, match the observed park before recovering any run."""
    if auto_release and auto_release.get("auto") == "scheduled_until":
        row = conn.execute("SELECT status FROM tasks WHERE id=? AND status='scheduled' AND scheduled_until=?",
                           (task_id, auto_release.get("until"))).fetchone()
    else:
        row = conn.execute("SELECT status FROM tasks WHERE id=? AND status IN ('blocked', 'scheduled')",
                           (task_id,)).fetchone()
    return row["status"] if row else None


def reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses: tuple[str, ...], now: int, note: str,
) -> None:
    """Close a leaked open run before a status flip so the invariant
    ``current_run_id IS NULL <=> run row terminal`` holds; no-op normally."""
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )
