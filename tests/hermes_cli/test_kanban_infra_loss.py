"""A worker the HOST lost (owner stall / restart) must not spend the card's failure budget.

25 Nov 2026: one gateway owner-loop stall killed 8 managed workers in two sweeps
("Kanban owner refused: Kanban execution unknown", ``worker_lost`` reason
``managed_worker_failed``); the systemic same-error breaker parked 6 of them at
failure_limit=1 after that single host event.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    return home


def _start_and_lose(conn, tid, host, n, *, reason="managed_worker_failed", code=1):
    """Claim a fresh run on this host, bind a dead pid, record the owner's ``worker_lost``."""
    claimed = kb.claim_task(conn, tid, claimer=f"{host}:w{n}")
    pid = 72000 + n * 10 + abs(hash(tid)) % 7
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    if reason:
        kb._append_event(conn, tid, "worker_lost", {"reason": reason}, run_id=claimed.current_run_id)
    conn.commit()
    kbd._record_worker_exit(pid, code << 8)
    return pid


def _gave_up(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)
    ).fetchone()[0]


def test_mass_managed_worker_loss_requeues_every_card_without_spending_failures(kanban_home):
    """6 simultaneous host losses in ONE sweep: all back to ready, 0 failures, no gave_up,
    no ``crashed`` wake event (one ``infra_requeued`` each instead)."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tids = [kb.create_task(conn, title=f"lane {i}", assignee="kepler") for i in range(6)]
        for i, tid in enumerate(tids):
            _start_and_lose(conn, tid, host, i)

        kbd.detect_crashed_workers(conn)
        assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []) == []
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert task.status == "ready", (tid, task.status)
            assert task.consecutive_failures == 0
            assert _gave_up(conn, tid) == 0
            kinds = [r[0] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?", (tid,)).fetchall()]
            assert kbd.INFRA_REQUEUED_EVENT in kinds and "crashed" not in kinds
            meta = json.loads(conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
            ).fetchone()[0])
            assert meta["infra_loss"] is True and meta["infra_losses"] == 1
            assert "managed worker lost" in (task.last_failure_error or "")


REFUSAL = "Kanban owner refused: Kanban execution unknown; retained by owner"


def _log(tid, text):
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER as TR
    path = kb.worker_log_path(tid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text.replace("<EXIT>", TR))


def test_owner_refused_output_alone_is_an_infra_loss(kanban_home):
    """No ``worker_lost`` row, but the submitter's own last words name the owner refusal."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tids = [kb.create_task(conn, title=f"t{i}", assignee="a") for i in range(3)]
        for i, tid in enumerate(tids):
            _log(tid, f"Session: local-x\n{REFUSAL}\n<EXIT>1\n")
            _start_and_lose(conn, tid, host, i, reason=None)
        kbd.detect_crashed_workers(conn)
        for tid in tids:
            task = kb.get_task(conn, tid)
            assert (task.status, task.consecutive_failures, _gave_up(conn, tid)) == ("ready", 0, 0)


def test_an_earlier_attempts_refusal_in_the_shared_log_does_not_classify_this_death(kanban_home):
    """Two attempts share one append-only log: attempt 1 was refused, attempt 2 died on a
    broken environment. Attempt 2 is a plain crash."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="env", assignee="a")
        _log(tid, f"{REFUSAL}\n<EXIT>1\nModuleNotFoundError: No module named 'agent'\n<EXIT>1\n")
        _start_and_lose(conn, tid, host, 0, reason=None)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        meta = json.loads(conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
        assert "infra_loss" not in meta


def test_a_mixed_sweep_never_leaks_infra_losses_into_the_systemic_tally(kanban_home, monkeypatch):
    """Two host losses + one plain crash with the SAME error head in one sweep: the plain
    card takes one failure and is not tripped (1 plain < 3 identical)."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        infra = [kb.create_task(conn, title=f"i{i}", assignee="a") for i in range(2)]
        plain = kb.create_task(conn, title="p", assignee="a")
        for i, tid in enumerate(infra):
            _start_and_lose(conn, tid, host, i)
        _start_and_lose(conn, plain, host, 5, reason="managed_worker_hello_timeout")
        kbd.detect_crashed_workers(conn)
        assert getattr(kbd.detect_crashed_workers, "_last_auto_blocked", []) == []
        assert kb.get_task(conn, plain).consecutive_failures == 1
        assert kb.get_task(conn, plain).status == "ready"
        assert all(kb.get_task(conn, t).consecutive_failures == 0 for t in infra)


def test_a_long_quota_window_neither_resets_nor_hides_the_infra_streak(kanban_home):
    """60 rate-limited runs between infra losses: the streak carries across them, and a
    non-infra run resets it."""
    limit = kbd._INFRA_LOSS_REQUEUE_LIMIT
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="quota", assignee="a")
        for n in range(limit):
            _start_and_lose(conn, tid, host, n)
            kbd.detect_crashed_workers(conn)
        for k in range(60):
            claimed = kb.claim_task(conn, tid, claimer=f"{host}:q{k}")
            kb._end_run(conn, tid, outcome="rate_limited", status="rate_limited", error="quota")
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, "
                         "worker_pid=NULL, current_run_id=NULL WHERE id=?", (tid,))
            conn.commit()
            assert claimed is not None
        assert kbd._prior_infra_losses(conn, tid) == limit
        _start_and_lose(conn, tid, host, 90)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).consecutive_failures == 1   # over the limit: counted
        # A plain (non-infra) run resets the streak.
        _start_and_lose(conn, tid, host, 91, reason="managed_worker_hello_timeout")
        kbd.detect_crashed_workers(conn)
        assert kbd._prior_infra_losses(conn, tid) == 0


def test_infra_losses_are_bounded_so_a_real_loop_still_surfaces(kanban_home):
    """After ``_INFRA_LOSS_REQUEUE_LIMIT`` consecutive host losses the next one is a plain
    crash: it counts and, at DEFAULT_FAILURE_LIMIT, gives up."""
    limit = kbd._INFRA_LOSS_REQUEUE_LIMIT
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="loops", assignee="a")
        for n in range(limit):
            _start_and_lose(conn, tid, host, n)
            kbd.detect_crashed_workers(conn)
            assert kb.get_task(conn, tid).consecutive_failures == 0
        _start_and_lose(conn, tid, host, limit)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 1
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='crashed' ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        assert json.loads(ev[0])["infra_loss_limit_reached"] == limit + 1
        _start_and_lose(conn, tid, host, limit + 1)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, tid).status == "blocked"


def test_plain_same_error_crashes_still_trip_the_systemic_breaker(kanban_home):
    """Unchanged: 3 identical non-infra crashes in one sweep (broken venv) trip at 1."""
    with kbc.connect() as conn:
        host = kb._claimer_id().split(":", 1)[0]
        tids = [kb.create_task(conn, title=f"s{i}", assignee="a") for i in range(3)]
        for i, tid in enumerate(tids):
            _start_and_lose(conn, tid, host, i, reason=None, code=2)
        kbd.detect_crashed_workers(conn)
        assert sorted(getattr(kbd.detect_crashed_workers, "_last_auto_blocked", [])) == sorted(tids)
