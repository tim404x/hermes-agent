"""Dispatch resume deadlines, parked-row selection, and dangling attempt recovery."""

import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Callable, Optional


GOAL_MODE_MAX_SCHEDULE_SECONDS = 12 * 3600
GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def goal_schedule_error(until: Optional[int], now: int) -> Optional[str]:
    if until is not None and 0 < until - now <= GOAL_MODE_MAX_SCHEDULE_SECONDS:
        return None
    return ("goal_mode tasks cannot be scheduled without a future until at most 12 hours away: "
            f"use kanban_block with kind in {sorted(GOAL_MODE_BLOCK_ALLOWED_KINDS)} for a genuine "
            "external blocker, or kanban_complete so the completion judge can evaluate it.")


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
                   claim_expires = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )


def build_unblock_audit_payload(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    auto_release: Optional[dict],
    new_status: str,
    resume_status: str,
    author: Optional[str],
    reason: Optional[str],
    add_comment_fn: Callable[[sqlite3.Connection, str, str, str], int],
    redact_fn: Callable[[Any], Any],
) -> dict[str, Any]:
    """Write any operator ``UNBLOCK:`` comment and build the ``unblocked`` event payload."""
    clean_reason = (reason or "").strip() or None
    if clean_reason is not None:
        from hermes_cli.kanban_swarm import redact_blackboard_comment
        clean_reason = (
            redact_blackboard_comment(clean_reason)
            or str(redact_fn(clean_reason)).strip()
            or None
        )
    comment_id = (
        add_comment_fn(conn, task_id, author or "operator", f"UNBLOCK: {clean_reason}")
        if clean_reason
        else None
    )
    payload: dict[str, Any] = dict(auto_release or {})
    if auto_release is not None and not payload.get("auto"):
        payload["auto"] = True
    if new_status != "ready" or resume_status != "ready" or auto_release == {}:
        payload.update(status=new_status, resume_status=resume_status)
    if comment_id is not None:
        payload["reason"] = clean_reason
        payload["comment_id"] = comment_id
    if auto_release is None:
        max_cid = conn.execute(
            "SELECT MAX(id) FROM task_comments WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        if max_cid is not None:
            payload["after_comment_id"] = int(max_cid)
    return payload


def specify_triage_updates(
    existing: sqlite3.Row,
    *,
    title: Optional[str],
    body: Optional[str],
    assignee: Optional[str],
) -> tuple[list[str], list[Any], list[str]]:
    """Compute SQL SET clauses, params, and changed field names for ``specify_triage_task``."""
    sets: list[str] = ["status = 'todo'"]
    params: list[Any] = []
    changed_fields: list[str] = []
    if title is not None and title.strip() != (existing["title"] or ""):
        sets.append("title = ?")
        params.append(title.strip())
        changed_fields.append("title")
    if body is not None and (body or "") != (existing["body"] or ""):
        sets.append("body = ?")
        params.append(body)
        changed_fields.append("body")
    if assignee is not None and assignee != (existing["assignee"] or None):
        sets.append("assignee = ?")
        params.append(assignee)
        changed_fields.append("assignee")
    return sets, params, changed_fields


def untriage_event_payload(
    prior: sqlite3.Row,
    reason: Optional[str],
    comment_id: Optional[int],
) -> dict[str, Any]:
    """Build the ``untriaged`` event payload with optional verified ``comment_id``."""
    payload: dict[str, Any] = {
        "prev_block_kind": prior["block_kind"],
        "prev_block_recurrences": prior["block_recurrences"],
        "reason": reason,
    }
    if comment_id is not None:
        payload["comment_id"] = comment_id
    return payload
