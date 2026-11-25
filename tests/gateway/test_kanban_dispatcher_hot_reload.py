"""Live settings and capacity diagnostics for the embedded dispatcher."""

import asyncio
import logging
import sqlite3
from functools import partial
from itertools import count
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import kanban_watchers, kanban_watchers_dispatcher as watchers
from hermes_cli import config, kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.config_backend import ConfigBackendUnavailable
from hermes_cli.config_read_errors import FailedConfigRead


@pytest.fixture
def live_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    kanban = {}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": kanban})
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda: 8)
    monkeypatch.setattr(kbd, "detect_crashed_workers", lambda conn, board: [])
    pids = count(100_000)
    spawn = partial(kbd.dispatch_once, spawn_fn=lambda task, workspace: next(pids))
    monkeypatch.setattr(kbd, "dispatch_once", spawn)
    dispatcher = watchers._KanbanDispatcher(kb, watchers._resolve_dispatcher_settings(kanban, kb))
    kb.init_db()
    with kbc.connect_closing(board="default") as conn:
        yield dispatcher, kanban, conn


def test_memory_cap_diagnosed_then_raised_live(live_board, caplog):
    dispatcher, kanban, conn = live_board
    for index in range(14):
        kb.create_task(conn, title=f"worker {index}", assignee="default")
    assert len(kbd.dispatch_once(conn, max_spawn=8, board="default").spawned) == 8
    running = conn.execute("SELECT worker_pid, claim_lock FROM tasks WHERE status='running'").fetchall()
    assert len(running) == 8 and all(pid and lock for pid, lock in running)

    caplog.set_level(logging.INFO, logger="gateway.run")
    [(slug, held)] = dispatcher.tick_once()
    assert slug == "default" and not held.spawned
    assert getattr(held, "capacity_held", None) == ("max_in_progress", 8, 8)
    warning = watchers._stuck_warning(105, [held], dispatcher.max_in_progress_source)
    assert "max_in_progress=8 (source: memory-default, running 8)" in warning
    assert "Check profile health" not in warning

    kanban["max_in_progress"] = 12
    [(_, resumed)] = dispatcher.tick_once()
    assert len(resumed.spawned) == 4
    assert kbd.count_running_tasks(conn) == 12
    [(_, full)] = dispatcher.tick_once()
    assert not full.spawned and full.capacity_held == ("max_in_progress", 12, 12)
    assert "max_in_progress=12 (source: config, running 12)" in watchers._stuck_warning(
        6, [full], dispatcher.max_in_progress_source,
    )
    change = "kanban dispatcher: max_in_progress changed 8 -> 12 (config edit applied live)"
    assert [rec.levelno for rec in caplog.records if rec.getMessage() == change] == [logging.INFO]


def test_live_interval_and_non_cap_setting_reach_dispatch(live_board, monkeypatch):
    dispatcher, kanban, _conn = live_board
    dispatch_once = kbd.dispatch_once
    limits = []

    def dispatch(conn, **kwargs):
        limits.append(kwargs["failure_limit"])
        return dispatch_once(conn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", dispatch)
    dispatcher.tick_once()
    kanban.update(dispatch_interval_seconds=9, failure_limit=7)
    dispatcher.tick_once()
    assert limits == [kb.DEFAULT_FAILURE_LIMIT, 7]
    assert dispatcher.settings.interval == 9


def test_memory_probe_recovers_without_config_edit(live_board, monkeypatch, caplog):
    _, kanban, conn = live_board
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda: None)
    dispatcher = watchers._KanbanDispatcher(kb, watchers._resolve_dispatcher_settings(kanban, kb))
    dispatcher.tick_once()
    assert dispatcher.settings.max_in_progress is None

    for index in range(9):
        kb.create_task(conn, title=f"worker {index}", assignee="default")
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda: 8)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        [(_, recovered)] = dispatcher.tick_once()
        assert len(recovered.spawned) == 8
        dispatcher.tick_once()
    assert sum("max_in_progress changed unset -> 8" in msg for msg in caplog.messages) == 1


@pytest.mark.parametrize("invalid", ["broken", 5.5, True, 0])
def test_quoted_max_spawn_applies_and_invalid_edit_keeps_it(live_board, caplog, invalid):
    dispatcher, kanban, conn = live_board
    for index in range(6):
        kb.create_task(conn, title=f"worker {index}", assignee="default")
    kanban["max_spawn"] = "4"
    [(_, result)] = dispatcher.tick_once()
    assert len(result.spawned) == 4

    kanban["max_spawn"] = invalid
    for _ in range(2):
        [(_, result)] = dispatcher.tick_once()
        assert not result.spawned and result.capacity_held == ("max_spawn", 4, 4)
    assert sum("kanban.max_spawn=" in msg for msg in caplog.messages) == 1
    kanban["max_spawn"] = 1
    assert dispatcher.tick_once_for_board("default").capacity_held == ("max_spawn", 4, 1)
    del kanban["max_spawn"]
    [(_, uncapped)] = dispatcher.tick_once()
    assert len(uncapped.spawned) == 2


@pytest.mark.parametrize("invalid", ["nan", "inf"])
def test_invalid_interval_uses_boot_default_or_last_live_value(live_board, invalid, caplog):
    dispatcher, kanban, _ = live_board
    boot = watchers._resolve_dispatcher_settings({"dispatch_interval_seconds": invalid, "max_spawn": "bad"}, kb)
    assert boot.interval == 60 and boot.max_spawn is None
    assert any("invalid kanban.max_spawn=" in msg for msg in caplog.messages)
    caplog.clear()
    kanban["dispatch_interval_seconds"] = 7
    dispatcher.tick_once()
    kanban["dispatch_interval_seconds"] = invalid
    for _ in range(2):
        dispatcher.tick_once()
        assert dispatcher.settings.interval == 7
    assert sum("invalid dispatch_interval_seconds=" in msg for msg in caplog.messages) == 1


def test_full_idle_board_does_not_explain_another_boards_queue(live_board):
    dispatcher, kanban, conn = live_board
    for index in range(2):
        kb.create_task(conn, title=f"running {index}", assignee="default")
    assert len(kbd.dispatch_once(conn, board="default").spawned) == 2
    kb.create_board("second")
    with kbc.connect_closing(board="second") as second:
        kb.create_task(second, title="running", assignee="default")
        assert len(kbd.dispatch_once(second, board="second").spawned) == 1
        kb.create_task(second, title="waiting", assignee="default")
    kanban.update(max_spawn=2, max_in_progress_per_profile=1)
    results = dict(dispatcher.tick_once())
    warning = watchers._stuck_warning(6, list(results.values()), dispatcher.max_in_progress_source, 1)
    assert "per_profile_capped=1" in warning
    assert "max_spawn" not in warning
    kb.create_task(conn, title="unassigned")
    kanban["default_assignee"] = "default"
    assert dispatcher.tick_once_for_board("default").capacity_held == ("max_spawn", 2, 2)


@pytest.mark.parametrize("cap", ["max_spawn", "max_in_progress"])
def test_full_board_only_reports_spawnable_reviews(live_board, monkeypatch, cap):
    dispatcher, kanban, conn = live_board
    kb.create_task(conn, title="running", assignee="default")
    assert len(kbd.dispatch_once(conn, board="default").spawned) == 1
    review = kb.create_task(conn, title="needs review", assignee="default")
    assert kb.request_review(conn, review, reviewer="default")
    kanban.update({cap: 1, "review_dispatch": False})
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": kanban})
    assert dispatcher.tick_once_for_board("default").capacity_held is None
    kanban["review_dispatch"] = True
    assert dispatcher.tick_once_for_board("default").capacity_held == (cap, 1, 1)


@pytest.mark.parametrize("broken", [
    {"kanban": []}, ConfigBackendUnavailable("backend offline"),
    FailedConfigRead({"kanban": {"auto_decompose": True}}, error=OSError("read failed")),
], ids=["malformed", "backend-down", "failed-read"])
def test_watcher_keeps_dispatching_and_sleeps_at_live_interval(live_board, monkeypatch, broken):
    boot = {"kanban": {"auto_decompose": False, "max_spawn": 2, "dispatch_interval_seconds": 30}}
    edited = {"kanban": {"auto_decompose": False, "max_spawn": 4, "dispatch_interval_seconds": 9}}
    snapshots = iter([boot, edited, broken, edited])
    current = next(snapshots)

    def load():
        if isinstance(current, ConfigBackendUnavailable):
            raise current
        return current

    monkeypatch.setattr(config, "load_config", load)
    monkeypatch.setattr(config, "load_config_readonly", load)
    monkeypatch.setattr(kanban_watchers, "_kanban_dispatch_allowed", lambda: True)
    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(kbd, "reap_worker_zombies", list)
    monkeypatch.setattr(kbc, "connect", lambda board: sqlite3.connect(kb.kanban_db_path(board)))
    dispatch = Mock(return_value=kbd.DispatchResult())
    decompose = Mock(return_value=0)
    monkeypatch.setattr(kbd, "dispatch_once", dispatch)
    monkeypatch.setattr(watchers._KanbanDispatcher, "auto_decompose_tick", decompose)
    runner = kanban_watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    intervals = []

    async def sleep_between_ticks(interval):
        nonlocal current
        intervals.append(interval)
        current = next(snapshots, None)
        runner._running = current is not None

    monkeypatch.setattr(runner, "_sleep_between_ticks", sleep_between_ticks)
    asyncio.run(runner._kanban_dispatcher_watcher())
    decompose.assert_not_called()
    assert [call.kwargs["max_spawn"] for call in dispatch.call_args_list] == [2, 4, 4, 4]
    assert intervals == [30, 9, 9, 9]


def test_suppression_names_board_cap_and_counts_profile_deferrals():
    held = kbd.DispatchResult(capacity_held=("max_spawn", 4, 3))
    deferred = kbd.DispatchResult(skipped_per_profile_capped=[("one", "default", 2), ("two", "default", 2)])
    summary = kbd.describe_suppression([held, None, deferred])
    assert "max_spawn full (running 4 >= 3)" in summary
    assert "per_profile_capped=2" in summary
