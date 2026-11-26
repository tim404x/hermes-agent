"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from hermes_cli.kanban_db_graph import decompose_triage_task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------
















def test_running_with_open_parents_fires_only_while_running():
    """A running card whose parent is not terminal is flagged; the same graph
    on a ready/todo card (the gate is holding it) and a done parent are not."""
    graph = {"parents": [{"id": "t_parent", "title": "p", "status": "todo"}], "children": []}
    diags = kd.compute_task_diagnostics(_task(status="running", started_at=100), [], [], graph=graph)
    assert [d.kind for d in diags] == ["running_with_open_parents"]
    assert diags[0].data["open_parents"] == [{"id": "t_parent", "status": "todo"}]
    assert "hermes kanban unlink t_parent t_demo00" in diags[0].actions[0].payload["command"]
    assert kd.compute_task_diagnostics(_task(status="todo"), [], [], graph=graph) == []
    done_graph = {"parents": [{"id": "t_parent", "title": "p", "status": "done"}], "children": []}
    assert kd.compute_task_diagnostics(_task(status="running"), [], [], graph=done_graph) == []


@pytest.mark.parametrize("run_id, linked_at", [(468, 575), (None, 100)], ids=["owned", "untagged-at-start"])
def test_dependency_handoff_link_by_current_run_does_not_warn(run_id, linked_at):
    task = _task(id="t_8689c05d", status="running", current_run_id=468)
    parent = {"id": "t_a5f76f8b", "status": "todo"}
    link = _event("linked", linked_at, parent=parent["id"], child=task["id"]) | {"run_id": run_id}
    diags = kd.compute_task_diagnostics(task, [link], [{"id": 468, "started_at": 100}], graph={"parents": [parent]})
    assert not any(diag.kind == "running_with_open_parents" for diag in diags)


@pytest.mark.parametrize("other_link", [
    _event("linked", 99, parent="t_gate", child="t_demo00"),
    _event("linked", 110, parent="t_gate", child="t_demo00") | {"run_id": 467},
    _event("linked", 110, parent="t_gate", child="t_other") | {"run_id": 468},
], ids=["before-retry", "foreign-run", "other-child"])
def test_running_with_open_parents_reports_only_non_handoff_edge(other_link):
    task = _task(status="running", started_at=10, current_run_id=468)
    runs = [{"id": 467, "started_at": 10}, {"id": 468, "started_at": 100}]
    gate = {"id": "t_gate", "status": "ready"}
    handoff = {"id": "t_handoff", "status": "todo"}
    link = _event("linked", 120, parent=handoff["id"], child=task["id"]) | {"run_id": 468}
    diags = kd.compute_task_diagnostics(task, [other_link, link], runs, graph={"parents": [handoff, gate]})
    warning, = (diag for diag in diags if diag.kind == "running_with_open_parents")
    assert warning.count == 1
    assert warning.data["open_parents"] == [gate]
    assert warning.actions[0].payload["command"] == "hermes kanban unlink t_gate t_demo00"


def test_repeating_a_link_to_a_reopened_parent_is_not_a_new_handoff():
    task = _task(status="running", current_run_id=468)
    parent = {"id": "t_reopened", "status": "ready"}
    graph = {"parents": [parent]}
    edge = {"parent": parent["id"], "child": task["id"]}
    link = _event("linked", 110, **edge) | {"run_id": 468}
    events = [_event("created", 50, parents=[parent["id"]]), link]
    warning, = kd.compute_task_diagnostics(task, events, [], graph=graph)
    assert warning.data["open_parents"] == [parent]

    events.extend([_event("unlinked", 115, **edge), link | {"created_at": 120}])
    assert not kd.compute_task_diagnostics(task, events, [], graph=graph)


def test_show_filtered_run_history_keeps_active_handoff_context(kanban_home):
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="work", assignee="worker")
        completed = kb.claim_task(conn, child).current_run_id
        assert kb.complete_task(conn, child, summary="first pass")
        assert kb.reopen_task(conn, child)
        active = kb.claim_task(conn, child).current_run_id
        parent = kb.create_task(conn, title="follow-up")
        kb.link_tasks(conn, parent, child, expected_child_run_id=active)

    command = f"show {child} --state-type outcome --state-name completed"
    output = kc.run_slash(command)
    assert "Running while" not in output
    assert f"Runs (1):\n  #{completed}" in output
    assert [run["id"] for run in json.loads(kc.run_slash(command + " --json"))["runs"]] == [completed]


def _fleet_open_parents(task_id):
    return {
        parent["id"] for row in json.loads(kc.run_slash("diagnostics --json")) if row["task_id"] == task_id
        for diag in row["diagnostics"] if diag["kind"] == "running_with_open_parents"
        for parent in diag["data"]["open_parents"]
    }


@pytest.mark.parametrize("started_at,linked_at,is_handoff", [
    ("2026-11-26T20:02:16Z", "2026-11-26T20:10:11Z", True),
    ("2026-11-26T20:02:16Z", "unreadable", False),
    ("unreadable", "2026-11-26T20:10:11Z", False),
    (100, float("inf"), False),
], ids=["iso", "invalid-link", "invalid-start", "nonfinite-link"])
def test_raw_timestamps_do_not_hide_open_parents(kanban_home, started_at, linked_at, is_handoff):
    with kbc.connect_closing() as conn:
        gate = kb.create_task(conn, title="original gate")
        assert kb.complete_task(conn, gate, summary="done")
        child = kb.create_task(conn, title="work", parents=[gate], assignee="worker")
        active = kb.claim_task(conn, child).current_run_id
        parent = kb.create_task(conn, title="handoff")
        kb.link_tasks(conn, parent, child, expected_child_run_id=active)
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (gate,))
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (started_at, child))
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (started_at, active))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'linked'", (linked_at, child))

    assert _fleet_open_parents(child) == ({gate} if is_handoff else {gate, parent})


def test_gc_then_relink_distinguishes_new_edge_from_no_op(kanban_home):
    with kbc.connect_closing() as conn:
        gate = kb.create_task(conn, title="original gate")
        assert kb.complete_task(conn, gate, summary="done")
        root = kb.create_task(conn, title="root", triage=True, parents=[gate])
        parent, = decompose_triage_task(conn, root, root_assignee="worker", children=[{"title": "subtask"}])
        assert kb.unlink_tasks(conn, parent, root)
        assert kb.complete_task(conn, root, summary="first pass")
        conn.execute("UPDATE task_events SET created_at = 1 WHERE task_id = ?", (root,))
        kb.gc_events(conn, older_than_seconds=1)
        assert [event.kind for event in kb.list_events(conn, root)] == ["decomposed"]

        assert kb.reopen_task(conn, root)
        active = kb.claim_task(conn, root).current_run_id
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (gate,))
        for dependency in (gate, parent, parent):
            kb.link_tasks(conn, dependency, root, expected_child_run_id=active)

    assert _fleet_open_parents(root) == {gate}


@pytest.mark.parametrize("started_at", [100, 99], ids=["claim-at-start", "claim-recorded-next-second"])
def test_same_second_untagged_links_use_current_claim_order(started_at):
    task = _task(status="running", current_run_id=468)
    before = {"id": "before", "status": "ready"}
    after = {"id": "after", "status": "ready"}
    events = [
        _event("claimed", 100) | {"run_id": 467, "id": 1},
        _event("linked", 100, parent="before", child=task["id"]) | {"id": 2},
        _event("claimed", 100) | {"run_id": 468, "id": 3},
        _event("linked", 100, parent="after", child=task["id"]) | {"id": 4},
    ]
    warning, = kd.compute_task_diagnostics(task, events, [{"id": 468, "started_at": started_at}],
                                           graph={"parents": [before, after]})
    assert warning.data["open_parents"] == [before]


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48








# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True
