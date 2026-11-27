"""Timed maintenance parks and human triage recovery after the 26 Nov GRIP/FLEET incident."""

import argparse
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from tools import kanban_tools


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn


def _running_task(conn, **kwargs):
    task_id = kb.create_task(conn, title="maintenance", assignee="worker", **kwargs)
    assert kb.claim_task(conn, task_id, claimer="worker") is not None
    return task_id


def _park_release_claim(conn, task_id, now):
    assert kb.schedule_task(conn, task_id, until=now + 60)
    assert kb.get_task(conn, task_id).status == "scheduled"
    assert kb.release_due_scheduled(conn, now=now + 61) == [task_id]
    assert kb.get_task(conn, task_id).status == "ready"
    assert kb.claim_task(conn, task_id, claimer="worker").status == "running"


def _block_unblock_claim(conn, task_id, kind):
    assert kb.block_task(conn, task_id, kind=kind, reason="waiting")
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id, claimer="worker") is not None


def _payloads(conn, task_id, kind):
    return [event.payload for event in kb.list_events(conn, task_id) if event.kind == kind]


def test_four_maintenance_parks_in_a_row_never_reach_triage(board):
    """26 Nov GRIP/FLEET: recurring maintenance is not an unblock loop."""
    task_id = _running_task(board)
    now = int(time.time())
    for window in range(4):
        _park_release_claim(board, task_id, now + window * 120)
        task = kb.get_task(board, task_id)
        assert (task.block_kind, task.block_recurrences, task.scheduled_until) == (None, 0, None)
    assert _payloads(board, task_id, "unblocked") == [
        {"auto": "scheduled_until", "until": now + window * 120 + 60} for window in range(4)
    ]


def test_maintenance_parks_do_not_feed_a_prior_transient_count(board):
    """26 Nov GRIP/FLEET: planned parks neither increment nor forgive a genuine failure."""
    task_id = _running_task(board)
    _block_unblock_claim(board, task_id, "transient")
    for window in range(3):
        _park_release_claim(board, task_id, window * 120)
        task = kb.get_task(board, task_id)
        assert (task.block_kind, task.block_recurrences) == ("transient", 1)


@pytest.mark.parametrize("interleave_park", [False, True])
def test_repeated_needs_input_block_still_goes_to_triage(board, interleave_park):
    """26 Nov GRIP/FLEET: a maintenance window must not hide an actual loop."""
    task_id = _running_task(board)
    _block_unblock_claim(board, task_id, "needs_input")
    if interleave_park:
        _park_release_claim(board, task_id, 100)
    assert kb.block_task(board, task_id, kind="needs_input", reason="still waiting")
    assert kb.get_task(board, task_id).status == "triage"
    assert _payloads(board, task_id, "block_loop_detected")[-1]["recurrences"] == 2


def test_untriage_keeps_body_byte_identical(board):
    """26 Nov GRIP/FLEET: preserve the spec, not the loop counter that re-triaged it in 26 s."""
    body = "Résumé 🛰️\r\n  preserve trailing spaces  \r\n\n"
    title = "FLEET 🧭\r\nΔ  \n"
    task_id = _running_task(board, body=body)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET title=? WHERE id=?", (title, task_id))
    _block_unblock_claim(board, task_id, "needs_input")
    assert kb.block_task(board, task_id, kind="needs_input", reason="still waiting")
    assert kb.get_task(board, task_id).status == "triage"
    assert kb.untriage_task(board, task_id, author="operator")
    task = kb.get_task(board, task_id)
    assert (task.title.encode(), task.body.encode(), task.assignee) == (title.encode(), body.encode(), "worker")
    assert (task.status, task.block_kind, task.block_recurrences) == ("ready", None, 0)
    assert _payloads(board, task_id, "untriaged") == [
        {"prev_block_kind": "needs_input", "prev_block_recurrences": 2}
    ]
    assert not kb.untriage_task(board, task_id)
    assert kb.claim_task(board, task_id, claimer="worker") is not None
    assert kb.block_task(board, task_id, kind="needs_input", reason="new question")
    assert kb.get_task(board, task_id).status == "blocked"


def test_release_deadline_precedence_and_manual_clear(board):
    """Explicit deadlines override markers; a missing timezone must never use host time."""
    reason = "quiet window (SCHEDULED_UNTIL=1970-01-01T01:01:40+01:00);."
    task_id = _running_task(board)
    assert kb.schedule_task(board, task_id, reason=reason, until=200)
    assert kb.release_due_scheduled(board, now=199) == []
    assert _payloads(board, task_id, "scheduled")[-1] == {"reason": reason, "until": 200}
    assert kb.unblock_task(board, task_id)
    assert kb.get_task(board, task_id).scheduled_until is None
    assert kb.schedule_task(board, task_id, reason=reason)
    assert kb.get_task(board, task_id).scheduled_until == 100
    assert kb.release_due_scheduled(board, now=100) == [task_id]
    assert kb.schedule_task(board, task_id, reason="SCHEDULED_UNTIL=1970-01-01T00:00:00")
    assert kb.get_task(board, task_id).scheduled_until is None
    assert kb.release_due_scheduled(board) == []
    assert kb.get_task(board, task_id).status == "scheduled"
    assert kb.unblock_task(board, task_id)


def test_until_input_formats():
    """Only explicit input accepts epoch seconds; malformed prose is not a timer."""
    for value in (60, 60.5, "60", "1970-01-01T00:01:00Z", "1970-01-01T01:01:00+01:00"):
        assert kb.coerce_until(value) == 60
    for value in ("not-a-time", "1970-01-01T01:01:00", " ", float("inf"), True):
        with pytest.raises(ValueError, match="until"):
            kb.coerce_until(value)
    assert kb.parse_scheduled_until("maintenance SCHEDULED_UNTIL=bad") is None


def test_release_regates_on_open_parent(board):
    """Automatic unblock uses the same dependency gate and audit fields as manual unblock."""
    parent = kb.create_task(board, title="open upstream")
    child = kb.create_task(board, title="downstream", parents=[parent])
    assert kb.schedule_task(board, child, until=0)
    assert kb.release_due_scheduled(board, now=1) == [child]
    assert kb.get_task(board, child).status == "todo"
    assert _payloads(board, child, "unblocked") == [
        {"status": "todo", "resume_status": "ready", "auto": "scheduled_until", "until": 0}
    ]


def test_stale_release_does_not_wake_a_new_park(board, monkeypatch):
    """A second connection re-parks between the dispatcher's due scan and its write transaction."""
    task_id = _running_task(board)
    assert kb.schedule_task(board, task_id, until=100)
    unblock = kb.unblock_task

    def reschedule_before_release(conn, selected_id, **kwargs):
        with kbc.connect_closing() as other:
            assert unblock(other, selected_id)
            assert kb.schedule_task(other, selected_id, until=200)
        return unblock(conn, selected_id, **kwargs)

    monkeypatch.setattr(kb, "unblock_task", reschedule_before_release)
    assert kb.release_due_scheduled(board, now=101) == []
    task = kb.get_task(board, task_id)
    assert (task.status, task.scheduled_until) == ("scheduled", 200)
    assert _payloads(board, task_id, "unblocked") == [None]


def test_due_park_recovers_a_dangling_run(board):
    """A legacy writer that parked without closing its run must not leave two live attempts."""
    task_id = _running_task(board)
    run_id = kb.get_task(board, task_id).current_run_id
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='scheduled', scheduled_until=1 WHERE id=?", (task_id,))
    assert kb.release_due_scheduled(board, now=1) == [task_id]
    assert kb.get_run(board, run_id).outcome == "reclaimed"
    task = kb.get_task(board, task_id)
    assert (task.status, task.current_run_id, task.scheduled_until) == ("ready", None, None)


def test_dispatch_reports_released_scheduled(board):
    """26 Nov GRIP/FLEET: the dispatcher owns the wake-up, not a worker's side timer."""
    task_id = _running_task(board)
    assert kb.schedule_task(board, task_id, until=int(time.time()) - 1)
    assert kbd.dispatch_once(board, dry_run=True).released_scheduled == [task_id]
    assert kb.get_task(board, task_id).status == "ready"


def test_failed_release_does_not_skip_promotion(board, monkeypatch, caplog):
    """A timer error is observable but does not stop the rest of a dispatch tick."""
    task_id = kb.create_task(board, title="pending promotion")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='todo' WHERE id=?", (task_id,))

    def fail_release(conn):
        raise sqlite3.OperationalError("timer scan unavailable")

    monkeypatch.setattr(kb, "release_due_scheduled", fail_release)
    result = kbd.dispatch_once(board, dry_run=True)
    assert result.released_scheduled == [] and result.promoted == 1
    assert "timer scan unavailable" in caplog.text


def test_legacy_scheduled_task_gains_nullable_timer(tmp_path):
    """Migrating an existing park must not invent a wake-up time."""
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as legacy:
        legacy.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                                created_at INTEGER NOT NULL);
            INSERT INTO tasks VALUES ('old', 'existing park', 'scheduled', 1);
        """)
    kb.init_db(path)
    with kbc.connect_closing(path) as conn:
        assert conn.execute("SELECT scheduled_until FROM tasks WHERE id='old'").fetchone()[0] is None
        assert kb.release_due_scheduled(conn) == []
        assert kb.unblock_task(conn, "old")
        assert kb.schedule_task(conn, "old", until=123)
        assert kb.get_task(conn, "old").scheduled_until == 123


@pytest.mark.parametrize("goal,seconds,marker,allowed", [
    (False, 60, False, True), (False, "invalid", False, False),
    (True, None, False, False), (True, -1, False, False), (True, 6 * 3600, False, True),
    (True, 12 * 3600, True, True), (True, 13 * 3600, False, False),
])
def test_tool_schedule_requires_valid_and_bounded_goal_timer(board, monkeypatch, goal, seconds, marker, allowed):
    """26 Nov GRIP/FLEET: bounded maintenance returns a goal to its completion judge."""
    task_id = _running_task(board, goal_mode=goal)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(board, task_id).current_run_id))
    until = int(time.time()) + seconds if isinstance(seconds, int) else seconds
    args = {"until": until} if until is not None else {}
    if marker:
        args = {"reason": "SCHEDULED_UNTIL=" + datetime.fromtimestamp(until, UTC).isoformat()}
    result = json.loads(kanban_tools._handle_schedule(args))
    task = kb.get_task(board, task_id)
    if allowed:
        assert result["ok"] and datetime.fromisoformat(result["scheduled_until"]).timestamp() == until
        assert (task.status, task.scheduled_until) == ("scheduled", until)
    else:
        assert ("goal_mode" if goal else "until") in result["error"]
        assert task.status == "running"


def test_cli_schedule_and_orchestrator_untriage(board, monkeypatch, capsys):
    """26 Nov GRIP/FLEET: bulk park validation precedes writes; only operators clear triage."""
    parser = argparse.ArgumentParser(prog="hermes")
    kanban_cli.build_parser(parser.add_subparsers())

    def cli(*words):
        return kanban_cli.kanban_command(parser.parse_args(["kanban", *words]))

    first, second = (_running_task(board) for _ in range(2))
    assert cli("schedule", first, "quiet", "--ids", second, "--until", "1970-01-01T00:01:00") != 0
    assert all(kb.get_task(board, tid).status == "running" for tid in (first, second))
    assert all(not kb.list_comments(board, tid) for tid in (first, second))
    assert cli("schedule", first, "quiet", "--ids", second, "--until", "60") == 0
    assert "1970-01-01T00:01:00" in capsys.readouterr().out
    assert all(kb.get_task(board, tid).scheduled_until == 60 for tid in (first, second))
    triaged = [kb.create_task(board, title="intact", triage=True) for _ in range(2)]
    monkeypatch.setenv("HERMES_KANBAN_TASK", first)
    assert cli("untriage", *triaged) != 0
    assert "orchestrator-only" in capsys.readouterr().err
    assert all(kb.get_task(board, tid).status == "triage" for tid in triaged)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert cli("untriage", *triaged, "--reason", "false alarm") == 0
    output = capsys.readouterr().out
    for tid in triaged:
        assert f"Untriaged {tid} -> todo" in output
        assert kb.get_task(board, tid).status == "ready"
        assert kb.list_comments(board, tid)[-1].body == "UNTRIAGE: false alarm"
    assert cli("untriage", triaged[0]) != 0
    assert f"cannot untriage {triaged[0]} (not in triage?)" in capsys.readouterr().err
