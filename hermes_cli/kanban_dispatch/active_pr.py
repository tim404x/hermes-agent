"""Active-PR respawn guard helpers: lift events, barriers, and audit comments."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Iterator, Optional

from hermes_cli import kanban_db as _kb


ACTIVE_PR_LIFT_EVENTS = (
    "assigned",
    "changes_requested",
    "review_reopened",
    "specified",
    "untriaged",
    "unblocked",
)
ACTIVE_PR_LIFT_WHERE = "AND kind IN (" + ", ".join(f"'{k}'" for k in ACTIVE_PR_LIFT_EVENTS) + ")"

_AUDIT_COMMENT_PREFIXES = {"untriaged": "UNTRIAGE", "unblocked": "UNBLOCK"}


def _expected_audit_bodies(prefix: str, reason: str) -> set[str]:
    from hermes_cli.kanban_swarm import redact_blackboard_comment

    raw = f"{prefix}: {reason}".strip()
    redacted = redact_blackboard_comment(raw) or str(_kb.redact_review_value(raw)).strip()
    return {raw, redacted}


def _comment_matches_audit(
    comment: sqlite3.Row,
    event_ts: int,
    expected_bodies: set[str],
) -> bool:
    cts = _kb._to_epoch(comment["created_at"])
    if cts is None or not (event_ts - 1 <= cts <= event_ts):
        return False
    return (_kb._lossy_text(comment["body"]) or "").strip() in expected_bodies


def _is_operator_unblock_payload(payload: Optional[str]) -> tuple[bool, Optional[dict[str, Any]]]:
    """Validate an ``unblocked`` event payload.

    Only genuine SQL ``NULL`` (``None``, legacy bare ``unblock_task``) is
    accepted without a JSON object; empty string ``""``, corrupt JSON,
    non-object JSON, truthy ``auto``, and non-operator ``source`` fail closed.
    """
    if payload is None:
        return True, None
    data = _kb._json_or(payload, None)
    if not isinstance(data, dict):
        return False, None
    if data.get("auto"):
        return False, None
    source = data.get("source")
    if source is not None and source != "operator":
        return False, None
    return True, data


def release_audit_comment_ids(conn: sqlite3.Connection, task_id: str) -> set[int]:
    """Comment ids proven to be written by operator ``untriage_task`` / ``unblock_task``.

    An operator's ``UNTRIAGE:`` or ``UNBLOCK:`` reason may cite the existing PR
    URL to tell the worker which PR to finish; excluding that verified audit
    comment prevents it from tying with the release event or falsely acting as
    a worker-opened PR. Any recorded ``comment_id`` is cross-checked against
    its comment row's timestamp and ``PREFIX: <reason>`` body so a malformed or
    forged payload can never hide a worker PR comment. Legacy pre-``comment_id``
    ``untriaged`` rows are matched at most once by body + timestamp.
    """
    ids: set[int] = set()
    rows = conn.execute(
        "SELECT kind, created_at, payload FROM task_events "
        "WHERE task_id = ? AND kind IN ('untriaged', 'unblocked') ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    if not rows:
        return ids

    comments = conn.execute(
        "SELECT id, body, created_at FROM task_comments WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    comments_by_id = {int(c["id"]): c for c in comments}

    for row in rows:
        kind = row["kind"]
        prefix = _AUDIT_COMMENT_PREFIXES[kind]
        if kind == "unblocked":
            ok, data = _is_operator_unblock_payload(row["payload"])
            if not ok or data is None:
                continue
        else:
            data = _kb._json_or(row["payload"], None)
            if not isinstance(data, dict):
                continue

        reason = data.get("reason")
        event_ts = _kb._to_epoch(row["created_at"])
        if not isinstance(reason, str) or not reason.strip() or event_ts is None:
            continue
        expected = _expected_audit_bodies(prefix, reason)

        cid = data.get("comment_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            comment = comments_by_id.get(cid)
            if comment is not None and _comment_matches_audit(comment, event_ts, expected):
                ids.add(cid)
            continue

        if kind == "untriaged":
            for c in comments:
                cand_id = int(c["id"])
                if cand_id in ids:
                    continue
                if _comment_matches_audit(c, event_ts, expected):
                    ids.add(cand_id)
                    break
    return ids


def _classify_barrier(kind: str, payload: Optional[str]) -> tuple[bool, bool]:
    """Return ``(is_triage_barrier, is_park_barrier)`` for one event row."""
    classifier = _BARRIER_CLASSIFIERS.get(kind)
    return classifier(payload) if classifier is not None else (False, False)


_BARRIER_CLASSIFIERS: dict[str, Callable[[Optional[str]], tuple[bool, bool]]] = {
    "block_loop_detected": lambda _p: (True, True),
    "blocked": lambda _p: (False, True),
    "gave_up": lambda _p: (False, True),
    "scheduled": lambda _p: (False, True),
    "imported": lambda p: (True, True) if _kb._json_dict(p).get("parked") is True else (False, False),
    "status": lambda p: (
        (True, True)
        if _kb._json_dict(p).get("status") == "triage"
        else ((False, True) if _kb._json_dict(p).get("status") in ("blocked", "scheduled") else (False, False))
    ),
}


def latest_barrier_event_ids(conn: sqlite3.Connection, task_id: str) -> tuple[int, int]:
    """Return ``(latest_triage_id, latest_park_id)`` from ``task_events``.

    - ``latest_triage_id``: newest event routing the card into ``triage``
      (``block_loop_detected``, ``status`` -> ``triage``, or ``imported`` with
      ``parked: true``). No lift event from before entering ``triage`` may
      authorize dispatch after triage.
    - ``latest_park_id``: newest event parking the card as ``blocked``,
      ``scheduled``, or ``triage`` (``blocked``, ``gave_up``, ``scheduled``,
      ``block_loop_detected``, ``imported`` with ``parked: true``, or
      ``status`` -> ``blocked``/``scheduled``/``triage``). An ``unblocked``
      event only lifts ``active_pr`` for the park episode it resolved, never
      past a subsequent block, schedule, or triage.
    """
    latest_triage_id = 0
    latest_park_id = 0
    for row in conn.execute(
        "SELECT id, kind, payload FROM task_events "
        "WHERE task_id = ? AND kind IN "
        "('blocked', 'block_loop_detected', 'gave_up', 'scheduled', 'imported', 'status') "
        "ORDER BY id DESC",
        (task_id,),
    ):
        eid = int(row["id"])
        is_triage, is_park = _classify_barrier(row["kind"], row["payload"])
        if is_triage:
            latest_triage_id = max(latest_triage_id, eid)
        if is_park:
            latest_park_id = max(latest_park_id, eid)
        if latest_triage_id and latest_park_id:
            break
    return latest_triage_id, latest_park_id


def _is_legacy_operator_specify(
    conn: Optional[sqlite3.Connection],
    task_id: Optional[str],
    event_ts: Optional[int],
    data: dict[str, Any],
) -> bool:
    """Distinguish a legacy ``specified`` event (no ``source`` key) using the
    audit comment written inside the same ``specify_triage_task`` transaction.

    - Matching audit comment with non-empty ``author != 'auto-decomposer'`` → True.
    - Matching audit comment with ``author == 'auto-decomposer'`` → False.
    - No matching audit comment (ambiguous legacy row) → False (fail closed).
    """
    if conn is None or not task_id or event_ts is None:
        return False
    changed = data.get("changed_fields")
    if not isinstance(changed, list) or not changed or not all(isinstance(f, str) and f for f in changed):
        return False
    expected_body = "Specified — updated " + ", ".join(changed) + " and promoted to todo."
    matched_authors: list[str] = []
    for row in conn.execute(
        "SELECT author, body, created_at FROM task_comments WHERE task_id = ?",
        (task_id,),
    ):
        cts = _kb._to_epoch(row["created_at"])
        if cts is None or not (event_ts - 1 <= cts <= event_ts):
            continue
        if _kb._lossy_text(row["body"]) == expected_body:
            matched_authors.append((_kb._lossy_text(row["author"]) or "").strip())
    return bool(matched_authors) and all(a and a != "auto-decomposer" for a in matched_authors)


def is_handoff_event(
    kind: str,
    payload: Optional[str],
    *,
    conn: Optional[sqlite3.Connection] = None,
    task_id: Optional[str] = None,
    event_ts: Optional[int] = None,
) -> bool:
    """Return True when ``kind`` is a deliberate handoff or explicit operator
    release/unblock that should lift ``active_pr``.

    - ``assigned``: only a move to a DIFFERENT non-null profile (never a no-op
      dev→dev reassign, unassign, or ``kanban.default_assignee``). Events
      without ``from`` (written before it was recorded) fail closed.
    - ``specified``: lifts when ``source == 'operator'``; automatic
      ``auto-decomposer`` sweeps fail closed. Legacy rows without ``source``
      must be proven operator-authored by their matching audit comment;
      ambiguous legacy rows fail closed.
    - ``unblocked``: lifts for explicit operator/orchestrator unblocks;
      automated wakes (``auto`` truthy, e.g. ``release_due_scheduled``),
      non-operator ``source``, empty string, and malformed payloads fail closed.
    - ``changes_requested``, ``review_reopened``, ``untriaged``: always lift.
    """
    if kind == "specified":
        data = _kb._json_or(payload, {})
        if not isinstance(data, dict):
            return False
        if "source" in data:
            return data.get("source") == "operator"
        return _is_legacy_operator_specify(conn, task_id, event_ts, data)
    if kind == "unblocked":
        ok, _ = _is_operator_unblock_payload(payload)
        return ok
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def _lift_covers_prs(
    kind: str,
    payload: Optional[str],
    event_ts: int,
    newest_pr_ts: int,
    active_pr_rows: list[tuple[int, int, bool]],
) -> bool:
    """Return True when a validated lift event covers all ``active_pr_rows``.

    For ``unblocked`` events that carry ``after_comment_id`` (recorded inside
    ``unblock_task``'s write txn), ``after_comment_id`` is authoritative: a
    higher worker PR comment id vetoes the lift even if the clock stepped back
    (``event_ts > newest_pr_ts``), while same-second operator comment + unblock
    lifts cleanly. Legacy ``unblocked`` rows without ``after_comment_id`` and
    all other lift kinds require ``event_ts > newest_pr_ts`` (strictly after).
    """
    if kind == "unblocked" and payload is not None:
        data = _kb._json_or(payload, None)
        if not isinstance(data, dict):
            return False
        if "after_comment_id" in data:
            after_cid = data.get("after_comment_id")
            if not isinstance(after_cid, int) or isinstance(after_cid, bool):
                return False
            return event_ts >= newest_pr_ts and all(
                ts_valid and cid <= after_cid for _, cid, ts_valid in active_pr_rows
            )
    return event_ts > newest_pr_ts


def is_active_pr_guarded(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    now: int,
    window: int,
    url_re: re.Pattern[str],
    rows_since: Callable[..., Iterator[tuple[int, Any]]],
) -> bool:
    """Return True when ``task_id`` has a recent PR comment with no valid lift."""
    pr_rows = [
        (t, int(c["id"]), _kb._to_epoch(c["created_at"]) is not None)
        for t, c in rows_since(
            conn, "task_comments", task_id, now - window,
            cols=", id, body", unreadable=now,  # duplicate-PR risk: fail closed
        )
        if url_re.search(_kb._lossy_text(c["body"]) or "")
    ]
    if not pr_rows:
        return False
    ignored_cids = release_audit_comment_ids(conn, task_id)
    active_pr_rows = [(t, cid, ts_valid) for t, cid, ts_valid in pr_rows if cid not in ignored_cids]
    if not active_pr_rows:
        return False
    newest_pr_ts = max(t for t, _, _ in active_pr_rows)
    latest_triage_id, latest_park_id = latest_barrier_event_ids(conn, task_id)
    for ets, e in rows_since(
        conn, "task_events", task_id, newest_pr_ts,
        ACTIVE_PR_LIFT_WHERE, cols=", id, kind, payload", strict=False,
    ):
        eid = int(e["id"])
        kind = e["kind"]
        if eid <= latest_triage_id:
            continue
        if kind == "unblocked" and eid <= latest_park_id:
            continue
        if not is_handoff_event(kind, e["payload"], conn=conn, task_id=task_id, event_ts=ets):
            continue
        if _lift_covers_prs(kind, e["payload"], ets, newest_pr_ts, active_pr_rows):
            return False
    return True
