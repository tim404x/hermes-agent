"""Regressions for PR #36 code review findings (Items 1-8)."""

from contextlib import asynccontextmanager
from datetime import timedelta
import sys

import pytest

from cron import dispatch_hold as D, executions as E, jobs as J, scheduler as S
from cron import scheduler_authority
from cron.occurrences import get_catch_up_occurrence_count
from cron.scheduler_authority import reconcile_pending as _real_reconcile
from cron.scheduler_provider import CronScheduler
from cron.update_refusal import attach_tick_slot, restore_refused_slot
from hermes_cli import gateway_client as client, gateway_runtime as runtime
from tests.cron.test_postcheck import ERROR, make_due
from tests.cron.test_tick_holds_agent_jobs_during_update import _fence
from tests.cron.test_xcron_fence_delays_never_changes import _bind_cron_home


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = _bind_cron_home(tmp_path / "home", monkeypatch)
    yield home
    S._shutdown_parallel_pool()


@pytest.fixture
def transport(cron_home, monkeypatch):
    from gateway import session_cron

    real = client.connect_gateway
    evidence = {"refuse": True, "errors": [], "submitted": [], "markers": []}
    side_effect = cron_home / "work"
    monkeypatch.setattr(session_cron, "owner_for_home", lambda home: None)
    monkeypatch.setattr(S, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(S, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(
        runtime,
        "discover_gateway_endpoint",
        lambda *a, **kw: runtime.GatewayDiscovery("absent"),
    )
    monkeypatch.setattr(
        runtime, "_observe_replacing_stale", lambda *a: pytest.fail("unfenced ensure")
    )

    class Owner:
        async def rpc(self, op, **params):
            if op == "cron.submit":
                evidence["submitted"].append(params["job_id"])
                with side_effect.open("a", encoding="utf-8") as f:
                    f.write(params["job_id"] + "\n")
                return {"session_id": "s", "admission_id": "a"}
            assert op == "cron.status"
            return {
                "status": "terminal",
                "result": [True, "# report\n", "observation made", None],
            }

    @asynccontextmanager
    async def connect():
        if not evidence["refuse"]:
            yield Owner()
            return
        assert any(j.get("fire_claim") for j in J.load_jobs())
        evidence["markers"].append(_fence(cron_home))
        if "after_claim" in evidence:
            evidence["after_claim"]()
        try:
            async with real():
                pytest.fail("fenced connection must not reach submit")
        except client.GatewayUnavailableError as exc:
            evidence["errors"].append(f"{type(exc).__name__}: {exc}")
            raise

    monkeypatch.setattr(client, "connect_gateway", connect)
    return evidence, side_effect


def test_intervening_fire_between_scan_and_claim_blocks_old_slot_restore(
    cron_home, monkeypatch, transport
):
    """Item 1: last_dispatch survives an intervening fire; a queued worker must not restore
    the older scan's slot after a newer fire claimed and booked."""
    ev, work = transport
    now = [J._hermes_now().replace(microsecond=0)]
    monkeypatch.setattr(J, "_hermes_now", lambda: now[0])

    jid, old_due = make_due("every 30m", timedelta(seconds=10), times=6, completed=0)
    due_jobs = J.get_due_jobs()
    assert [j["id"] for j in due_jobs] == [jid]
    scanned = due_jobs[0]
    J.advance_next_runs([jid])

    # An intervening fire claims, runs, and books before the queued worker claims.
    now[0] += timedelta(minutes=5)
    intervening = J.claim_job_for_fire(jid, return_job=True)
    assert isinstance(intervening, dict)
    assert J.mark_job_run(
        jid, True, expected_fire_owner=intervening["fire_claim"]["by"]
    )
    after_intervening = J.get_job(jid)
    newer_next = after_intervening["next_run_at"]
    assert newer_next != old_due
    assert after_intervening["last_dispatch"] == scanned["last_dispatch"]

    # Queued worker now starts and hits the update fence before cron.submit.
    execution = E.create_execution(
        jid, source="builtin", scheduled_instant=scanned.get("_scheduled_instant")
    )
    queued_job = dict(scanned, execution_id=execution["id"])
    assert S._process_due_job(queued_job, adapters=None, loop=None, verbose=False) is True

    row = J.get_job(jid)
    assert not work.exists() and ev["errors"] == [ERROR]
    assert row["next_run_at"] == newer_next, (
        f"queued worker restored older scan's slot {old_due} over newer fire's {newer_next}"
    )
    assert not row.get("fire_claim")
    assert row["repeat"]["completed"] == 1
    assert (row.get("last_fire_error") or {}).get("update_slot") != old_due
    assert E.get_execution(execution["id"])["status"] == "cancelled"


@pytest.mark.parametrize("reason", ["rescheduled", "run_claim_changed", "unprecharged"])
def test_oneshot_repeat_decrement_gated_on_plan_and_actual_precharge(
    cron_home, monkeypatch, reason
):
    """Item 2: restore_refused_slot must not decrement repeat.completed when the plan/run_claim
    changed or when this worker did not actually precharge repeat.completed."""
    once = (J._hermes_now() + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    jid, _due = make_due(once, timedelta(seconds=10), times=1, completed=0)

    due_jobs = J.get_due_jobs()
    assert [j["id"] for j in due_jobs] == [jid]
    scanned = due_jobs[0]
    claimed = J.claim_job_for_fire(jid, return_job=True)
    assert isinstance(claimed, dict)
    attach_tick_slot(claimed, scanned)

    if reason == "rescheduled":
        assert J.claim_dispatch(jid) is True
        assert J.get_job(jid)["repeat"]["completed"] == 1
        new_at = (J._hermes_now() + timedelta(hours=2)).replace(microsecond=0).isoformat()
        J.update_job(jid, {"schedule": new_at})
        expected_next = J.get_job(jid)["next_run_at"]
    elif reason == "run_claim_changed":
        assert J.claim_dispatch(jid) is True
        assert J.get_job(jid)["repeat"]["completed"] == 1
        rows = J.load_jobs()
        next(j for j in rows if j["id"] == jid)["run_claim"] = {
            "at": J._hermes_now().isoformat(),
            "by": "other-host:9999",
        }
        J.save_jobs(rows)
        expected_next = J.get_job(jid)["next_run_at"]
    else:
        # Simulate another writer bumping completed=1 without this worker calling claim_dispatch.
        rows = J.load_jobs()
        next(j for j in rows if j["id"] == jid)["repeat"]["completed"] = 1
        J.save_jobs(rows)
        expected_next = J.get_job(jid)["next_run_at"]

    detail = restore_refused_slot(claimed)
    row = J.get_job(jid)
    assert row["repeat"]["completed"] == 1, f"repeat.completed wrongly decremented ({reason}): {detail}"
    if reason in ("rescheduled", "run_claim_changed"):
        assert "current plan left alone" in detail
        assert row["next_run_at"] == expected_next


def test_provider_claimed_queued_run_now_preserves_future_oneshot_after_refusal(
    cron_home, transport
):
    """Item 3: a provider claiming a queued run-now must capture manual_run_restore_at under
    the claim lock so an update refusal restores it and retry keeps the future reminder."""
    ev, work = transport
    future_reminder = (J._hermes_now() + timedelta(hours=1)).replace(microsecond=0).isoformat()
    job = J.create_job(prompt="future reminder", schedule=future_reminder, repeat=1, deliver="local")
    jid = job["id"]
    stored_future = J.get_job(jid)["next_run_at"]

    triggered = J.trigger_job(jid)
    assert triggered is not None
    queued_manual_at = triggered["manual_run_at"]
    assert triggered["manual_run_restore_at"] == stored_future

    class TestProvider(CronScheduler):
        name = "test-provider"

        def start(self, *, adapters=None, loop=None):
            return None

        def stop(self):
            return None

    provider = TestProvider()
    assert provider.fire_due(jid) is True
    assert ev["errors"] == [ERROR] and not work.exists()

    after_refusal = J.get_job(jid)
    assert after_refusal["next_run_at"] == queued_manual_at
    assert after_refusal["manual_run_at"] == queued_manual_at
    assert after_refusal.get("manual_run_restore_at") == stored_future
    assert after_refusal["repeat"]["completed"] == 0

    ev["markers"][-1].unlink()
    ev["refuse"] = False
    assert provider.fire_due(jid) is True

    final = J.get_job(jid)
    assert work.read_text().splitlines() == [jid]
    assert final["repeat"]["completed"] == 0, "manual retry spent the one-shot's only repeat"
    assert final["enabled"] is True and final["state"] == "scheduled"
    assert final["next_run_at"] == stored_future


@pytest.mark.parametrize(
    "failure_mode",
    ["journal_write_only", "journal_write_oserror", "store_runtime_error"],
)
def test_refusal_store_and_journal_failures_recover_without_gateway_restart(
    cron_home, monkeypatch, transport, failure_mode
):
    """Item 4: both refusal-journal write OSError (with or without a concurrent store save error)
    and job-store RuntimeError must settle the execution out of 'running' and durably recover the
    slot without restarting the gateway."""
    import utils

    ev, work = transport
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", _real_reconcile)
    once = (J._hermes_now() + timedelta(minutes=5)).replace(microsecond=0).isoformat()
    jid, due = make_due(once, timedelta(seconds=10), times=1, completed=0)

    real_save = J.save_jobs
    real_write = utils.atomic_json_write
    injected = {"journal": 0, "save": 0}

    def fail_journal(path, data, *args, **kwargs):
        if "admissions" in str(path) and injected["journal"] == 0:
            injected["journal"] += 1
            raise OSError("injected: admissions directory read-only")
        return real_write(path, data, *args, **kwargs)

    def fail_rollback_save(rows, **kwargs):
        if injected["save"] == 0 and any(
            j.get("last_fire_error", {}).get("update_slot") == due for j in rows
        ):
            injected["save"] += 1
            if failure_mode == "store_runtime_error":
                raise RuntimeError("Cron database corrupted; refusing to overwrite jobs.json")
            raise OSError("injected: jobs.json temporarily read-only")
        return real_save(rows, **kwargs)

    def arm_faults():
        if failure_mode in ("journal_write_only", "journal_write_oserror"):
            monkeypatch.setattr(utils, "atomic_json_write", fail_journal)
        if failure_mode in ("journal_write_oserror", "store_runtime_error"):
            monkeypatch.setattr(J, "save_jobs", fail_rollback_save)

    ev["after_claim"] = arm_faults

    S.tick(verbose=False, sync=True)

    assert not work.exists()
    if failure_mode == "journal_write_only":
        assert injected["journal"] == 1 and injected["save"] == 0
        assert not list((cron_home / "cron/admissions").glob("*.json"))
        assert E.list_pending_refusals() == []
    elif failure_mode == "journal_write_oserror":
        assert injected["journal"] == 1 and injected["save"] == 1
        assert not list((cron_home / "cron/admissions").glob("*.json"))
        assert len(E.list_pending_refusals()) == 1
    else:
        assert injected["save"] == 1
        assert list((cron_home / "cron/admissions").glob("*.json"))

    rows = E.list_executions(job_id=jid)
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled", f"execution left unsettled: {rows[0]}"
    assert not E.execution_owner_live(rows[0]["id"], jid)

    # Same process (no gateway restart): next tick replays rollback while marker is still active.
    S.tick(verbose=False, sync=True)
    replayed = J.get_job(jid)
    assert replayed["next_run_at"] == due
    assert replayed["repeat"]["completed"] == 0
    assert not replayed.get("fire_claim") and not replayed.get("run_claim")
    assert not list((cron_home / "cron/admissions").glob("*.json"))
    assert E.list_pending_refusals() == []

    # Lift fence: job fires once and completes cleanly.
    ev["markers"][-1].unlink()
    ev["refuse"] = False
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work.read_text().splitlines() == [jid]
    assert J.get_job(jid)["repeat"]["completed"] == 1


@pytest.mark.platforms("linux")
def test_detached_worker_death_after_refusal_journal_restores_slot(
    cron_home, monkeypatch, tmp_path
):
    """Item 5: if a detached worker dies after writing the refusal journal but before restoring
    the job, the waiter must recognize the verified refusal journal instead of booking a
    provisional unknown failure that clears the fire claim."""
    from tools.process_registry import GatewayChildDispatch

    monkeypatch.setattr(scheduler_authority, "reconcile_pending", _real_reconcile)
    monkeypatch.setattr(S, "_maybe_run_worktree_maintenance", lambda: None)

    jid, due = make_due("every 30m", timedelta(seconds=10), times=6, completed=5)

    worker_script = tmp_path / "die_after_journal_worker.py"
    worker_script.write_text(
        """
import os, sys
from pathlib import Path
import cron.scheduler as S
import cron.update_refusal as UR

args = sys.argv
payload = Path(args[args.index("--external-worker-file") + 1])
ack = Path(args[args.index("--ack-file") + 1])

# Crash hard right after settle_refusal writes the refusal journal, before jobs.json restore.
UR.restore_refused_slot = lambda *a, **kw: os._exit(137)

S._run_external_worker_payload(payload, ack)
""",
        encoding="utf-8",
    )

    markers = []
    delivered = []
    monkeypatch.setattr(
        S, "_deliver_crash_failure", lambda *a, **kw: delivered.append(a) or (None, "suppressed")
    )

    def dispatch(command, **kw):
        assert J.get_job(jid)["fire_claim"]["by"]
        markers.append(_fence(cron_home))
        # Replace "-m cron.scheduler" with our worker wrapper keeping payload/ack args.
        idx = command.index("--external-worker-file")
        argv = [sys.executable, str(worker_script), *command[idx:]]
        return GatewayChildDispatch("degraded", argv)

    monkeypatch.setattr("tools.process_registry.restart_safe_gateway_child_argv", dispatch)

    S.tick(verbose=False, sync=True)

    row = J.get_job(jid)
    assert delivered == [], "verified refusal must not deliver a crash notice"
    assert not list((cron_home / "cron/admissions").glob("*.notice"))
    assert row["last_status"] is None and row["failure_streak"] == 0
    assert row["repeat"]["completed"] == 5
    assert row["next_run_at"] == due, "slot must be restored, not consumed by provisional failure"
    assert not row.get("fire_claim")
    assert not list((cron_home / "cron/admissions").glob("*.json"))
    exec_rows = E.list_executions(job_id=jid)
    assert len(exec_rows) == 1 and exec_rows[0]["status"] == "cancelled", exec_rows

    # Fence lifts: job runs cleanly once.
    markers[-1].unlink()
    work = []
    monkeypatch.setattr(S, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(
        scheduler_authority,
        "run_canonical_job",
        lambda job, **kw: (work.append(job["id"]) or True, "# ok", "ok", None),
    )
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work == [jid] and J.get_job(jid)["repeat"]["completed"] == 6


def test_record_unknown_worker_outcome_honors_verified_refusal_journal(cron_home):
    """Item 5 (concurrent sweep race): even if dead-owner sweep already marked the execution
    unknown before the waiter checked, record_unknown_worker_outcome must not provisionally fail
    or clear the claim when a verified refusal journal exists."""
    from cron.scheduler_worker_failure import record_unknown_worker_outcome

    jid, due = make_due("every 30m", timedelta(seconds=10), times=6, completed=5)
    due_jobs = J.get_due_jobs()
    scanned = due_jobs[0]
    J.advance_next_runs([jid])
    claimed = J.claim_job_for_fire(jid, return_job=True)
    attach_tick_slot(claimed, scanned)
    row = E.create_execution(jid, source="builtin")
    eid = row["id"]
    claimed["execution_id"] = eid
    E.give_up_execution(eid, reason="simulated concurrent unknown sweep")

    # Write refusal journal only (as if worker died before restore_refused_slot).
    from utils import atomic_json_write
    from cron.scheduler_authority import journal_path

    jpath = journal_path(jid, eid)
    jpath.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        jpath,
        {
            "params": {"job_id": jid, "request_id": eid},
            "update_refused": {
                k: claimed[k]
                for k in (
                    "id",
                    "execution_id",
                    "fire_claim",
                    "run_claim",
                    "schedule",
                    "timezone",
                    "repeat",
                    "next_run_at",
                    "manual_run_at",
                    "_refused_slot",
                )
                if k in claimed
            },
        },
        mode=0o600,
    )

    assert record_unknown_worker_outcome(claimed) is True

    after = J.get_job(jid)
    assert after["last_status"] is None and after["failure_streak"] == 0
    assert after["repeat"]["completed"] == 5
    assert after["next_run_at"] == due
    assert not after.get("fire_claim")
    assert E.get_execution(eid)["status"] == "cancelled"
    assert not jpath.exists()
    assert not jpath.with_suffix(".notice").exists()


def test_refusal_replay_two_days_later_does_not_revive_expired_oneshot(
    cron_home, monkeypatch, transport
):
    """Item 6: replay must preserve the original refusal observation time, not date the update
    witness at recovery; replaying two days later must let an expired one-shot retire."""
    ev, work = transport
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", _real_reconcile)
    t0 = J._hermes_now().replace(microsecond=0)
    now = [t0]
    monkeypatch.setattr(J, "_hermes_now", lambda: now[0])
    monkeypatch.setattr(D, "time", type("Clock", (), {"time": lambda: now[0].timestamp()}))

    once = (t0 + timedelta(minutes=5)).isoformat()
    jid, due = make_due(once, timedelta(seconds=10), times=1, completed=0)

    real_save = J.save_jobs
    blocked = []

    def fail_once(rows, **kwargs):
        if not blocked and any(
            j.get("last_fire_error", {}).get("update_slot") == due for j in rows
        ):
            blocked.append(True)
            raise OSError("injected: store unavailable at refusal")
        return real_save(rows, **kwargs)

    ev["after_claim"] = lambda: monkeypatch.setattr(J, "save_jobs", fail_once)
    S.tick(verbose=False, sync=True)
    assert blocked == [True] and not work.exists()
    assert list((cron_home / "cron/admissions").glob("*.json"))

    # Two days pass with NO fenced ticks; fence is gone and gateway is back.
    ev["markers"][-1].unlink()
    ev["refuse"] = False
    now[0] = t0 + timedelta(days=2)

    S.tick(verbose=False, sync=True)

    assert not work.exists(), "2-day-old expired one-shot was revived by replay timestamp"
    assert J.get_job(jid) is None, "expired one-shot should be retired, not kept eligible"
    assert not list((cron_home / "cron/admissions").glob("*.json"))


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("gateway_state", ["absent", "draining"])
def test_long_headless_update_with_non_ready_gateway_preserves_oneshot(
    cron_home, monkeypatch, gateway_state
):
    """Item 7: on a headless tick, the update fence must run before the generic gateway
    readiness gate so a non-ready gateway during an update does not wipe the update witness."""
    t0 = J._hermes_now().replace(microsecond=0)
    now = [t0]
    submitted = []
    gw_state = [gateway_state]

    monkeypatch.setattr(J, "_hermes_now", lambda: now[0])
    monkeypatch.setattr(D, "time", type("Clock", (), {"time": lambda: now[0].timestamp()}))
    monkeypatch.setattr(
        runtime,
        "discover_gateway_endpoint",
        lambda home, timeout=5.0: runtime.GatewayDiscovery(gw_state[0]),
    )
    monkeypatch.setattr(
        scheduler_authority,
        "run_canonical_job",
        lambda job, **kw: (submitted.append(job["id"]) or True, "# ok\n", "ok", None),
    )

    one = J.create_job(
        prompt="headless reminder",
        schedule=(t0 + timedelta(seconds=60)).isoformat(),
        name="once-headless",
        deliver="local",
    )["id"]
    due = J.get_job(one)["next_run_at"]

    marker = _fence(cron_home)
    for minute in range(7):  # 6+ minutes > 120s ONESHOT_GRACE_SECONDS
        now[0] = t0 + timedelta(minutes=minute)
        S.tick(verbose=False, sync=True, headless=True)

    held = J.get_job(one)
    assert submitted == []
    assert held is not None, "headless tick retired one-shot during fenced update"
    assert (held.get("last_fire_error") or {}).get("update_slot") == due
    assert D.read(cron_home / "cron") is not None

    marker.unlink()
    gw_state[0] = "ready"
    now[0] += timedelta(seconds=1)
    S.tick(verbose=False, sync=True, headless=True)
    S.tick(verbose=False, sync=True, headless=True)

    assert submitted == [one], "continuously witnessed headless one-shot must fire once after update"


def test_recovered_catchup_completion_books_count_and_missed_rows_idempotently(
    cron_home, monkeypatch
):
    """Item 8: catch-up accounting must be persisted with the execution and booked idempotently
    on ordinary or recovered completion, and never booked on a pre-submit refusal."""
    from gateway import session_cron

    monkeypatch.setattr(scheduler_authority, "reconcile_pending", _real_reconcile)
    monkeypatch.setattr(session_cron, "owner_for_home", lambda home: None)
    monkeypatch.setattr(S, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(S, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(scheduler_authority, "RECONNECT_BUDGET_S", 0.05, raising=False)
    monkeypatch.setattr(scheduler_authority, "RECONNECT_FIRST_DELAY_S", 0.01, raising=False)
    monkeypatch.setattr(
        runtime,
        "discover_gateway_endpoint",
        lambda *a, **kw: runtime.GatewayDiscovery("absent"),
    )

    jid, due = make_due("every 1h", timedelta(hours=3, minutes=30), times=5, completed=0)

    mode = {"phase": "refuse"}
    submitted_job = {}
    markers = []
    real_connect = client.connect_gateway

    class Peer:
        async def rpc(self, op, **params):
            if op == "cron.submit":
                submitted_job["job"] = J.get_job(jid)
                return {"session_id": "s1", "admission_id": "a1"}
            if op == "cron.status":
                raise client.GatewayClientError(client.DISCONNECTED)
            if op == "cron.recover":
                return {
                    "status": "terminal",
                    "session_id": "s1",
                    "admission_id": "a1",
                    "job": submitted_job["job"],
                    "result": [True, "# caught up\n", "caught up", None],
                }
            raise AssertionError(f"unexpected rpc {op}")

    @asynccontextmanager
    async def connect():
        if mode["phase"] == "refuse":
            markers.append(_fence(cron_home))
            async with real_connect():
                pytest.fail("must refuse under update fence")
        if mode["phase"] == "drop_after_submit":
            mode["phase"] = "down"
            yield Peer()
            return
        if mode["phase"] == "down":
            raise client.GatewayUnavailableError("Gateway stopped: not_ready", "stopped")
        yield Peer()

    monkeypatch.setattr(client, "connect_gateway", connect)

    # Phase 1: pre-submit refusal must NOT book catch-up count or missed rows.
    S.tick(verbose=False, sync=True)
    assert get_catch_up_occurrence_count() == 0
    assert [r for r in E.list_executions(job_id=jid) if r["status"] == "missed"] == []
    assert J.get_job(jid)["next_run_at"] == due
    markers[-1].unlink()

    # Phase 2: submit succeeds, socket drops, worker gives up as unknown.
    mode["phase"] = "drop_after_submit"
    S.tick(verbose=False, sync=True)
    assert J.get_job(jid)["state"] == "paused"
    assert get_catch_up_occurrence_count() == 0
    assert [r for r in E.list_executions(job_id=jid) if r["status"] == "missed"] == []

    # Phase 3: owner reports terminal on reconcile_pending -> catch-up booked once!
    mode["phase"] = "recover"
    S.tick(verbose=False, sync=True)

    assert J.get_job(jid)["state"] == "scheduled"
    assert J.get_job(jid)["last_status"] == "ok"
    assert get_catch_up_occurrence_count() == 1
    missed_rows = [r for r in E.list_executions(job_id=jid) if r["status"] == "missed"]
    assert len(missed_rows) == 1
    assert "missed \u00d73" in (missed_rows[0].get("error") or "")

    # Verify idempotency: re-booking the same execution does not double-count.
    completed_row = next(r for r in E.list_executions(job_id=jid) if r["status"] == "completed")
    from cron.missed_runs import book_catch_up

    book_catch_up(J.get_job(jid), (4, due, False), execution_id=completed_row["id"])
    assert get_catch_up_occurrence_count() == 1
    assert len([r for r in E.list_executions(job_id=jid) if r["status"] == "missed"]) == 1
