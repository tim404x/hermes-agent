"""A live update marker appears *inside* the worker, after scan, advance and fire claim."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from cron import executions as E, jobs as J, scheduler as S
from cron.occurrences import get_catch_up_occurrence_count
from cron.scheduler_authority import reconcile_pending as _real_reconcile
from hermes_cli import gateway_client as client, gateway_runtime as runtime
from tests.cron.test_tick_holds_agent_jobs_during_update import _fence, cron_home

ERROR = "GatewayUnavailableError: Gateway draining: update_paused"


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
    # The connect path MUST observe the fence; it may not ensure/start an absent gateway.
    monkeypatch.setattr(
        runtime, "_observe_replacing_stale", lambda *a: pytest.fail("unfenced ensure")
    )

    class Owner:
        async def rpc(self, op, **params):
            if op == "cron.submit":
                evidence["submitted"].append(params["job_id"])
                with side_effect.open("a") as f:
                    f.write(params["job_id"] + "\n")
                return {"session_id": "s", "admission_id": "a"}
            assert op == "cron.status"
            if evidence.get("disconnect"):
                raise client.GatewayUnavailableError(
                    "Gateway draining: update_paused", "draining", "update_paused"
                )
            if evidence.get("failed"):
                jid = evidence["submitted"][-1]
                return {
                    "status": "terminal",
                    "result": [
                        False,
                        f"# Cron Job: {jid} (FAILED)\n\n{ERROR}\n",
                        "",
                        ERROR,
                    ],
                }
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


def make_due(schedule, age, times=6, completed=5):
    jid = J.create_job(
        prompt="local verification", schedule=schedule, repeat=times, deliver="local"
    )["id"]
    rows = J.load_jobs()
    row = next(j for j in rows if j["id"] == jid)
    row["next_run_at"] = (J._hermes_now() - age).isoformat()
    row["repeat"]["completed"] = completed
    J.save_jobs(rows)
    return jid, row["next_run_at"]


@pytest.mark.parametrize("kind", ["interval", "catchup", "once"])
def test_two_postcheck_refusals_keep_last_repeat_then_release_once(
    cron_home, transport, kind
):
    ev, work = transport
    once = (J._hermes_now() + timedelta(minutes=5)).isoformat()
    jid, due = make_due(
        once if kind == "once" else "every 30m",
        timedelta(hours=3) if kind == "catchup" else timedelta(seconds=10),
        times=1 if kind == "once" else 6,
        completed=0 if kind == "once" else 5,
    )
    before = J.get_job(jid)
    for n in (1, 2):
        S.tick(verbose=False, sync=True)
        assert ev["errors"] == [ERROR] * n and not work.exists()
        row = J.get_job(jid)
        for key in (
            "repeat",
            "next_run_at",
            "enabled",
            "state",
            "last_run_at",
            "last_status",
            "failure_streak",
        ):
            assert row.get(key) == before.get(key), (key, row)
        assert not row.get("fire_claim") and not row.get("run_claim")
        assert row["last_fire_error"]["detail"] and not list(
            J.OUTPUT_DIR.glob(f"{jid}/*.md")
        )
        assert not list((cron_home / "cron/admissions").glob("*.json"))
        assert get_catch_up_occurrence_count() == 0
        assert all(x["status"] == "cancelled" for x in E.list_executions(job_id=jid))
        S.tick(
            verbose=False, sync=True
        )  # the still-active fence is now visible to the precheck
        ev["markers"][-1].unlink()
    ev["refuse"] = False
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work.read_text().splitlines() == [jid]
    rows = E.list_executions(job_id=jid)
    assert [x["scheduled_instant"] for x in rows if x["status"] == "completed"] == [due]
    assert len([x for x in rows if x["status"] == "cancelled"]) == 2
    assert get_catch_up_occurrence_count() == (1 if kind == "catchup" else 0)
    row = J.get_job(jid)
    assert (
        not row["enabled"]
        and row["last_status"] == "ok"
        and row["repeat"]["completed"] == row["repeat"]["times"]
    )


def test_identical_error_after_real_work_is_booked(cron_home, transport):
    ev, work = transport
    ev.update(refuse=False, failed=True)
    jid, _ = make_due("every 30m", timedelta(seconds=10))
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    row = J.get_job(jid)
    assert (
        not row["enabled"]
        and row["repeat"]["completed"] == 6
        and row["last_status"] == "error"
    )
    assert work.read_text().splitlines() == [jid]
    assert [x["status"] for x in E.list_executions(job_id=jid)] == ["failed"]
    assert (
        next(J.OUTPUT_DIR.glob(f"{jid}/*.md")).read_text().rstrip()
        == f"# Cron Job: {jid} (FAILED)\n\n{ERROR}"
    )


def test_same_typed_disconnect_after_submit_is_unknown_not_retried(
    cron_home, transport
):
    ev, work = transport
    ev.update(refuse=False, disconnect=True)
    jid, _ = make_due("every 30m", timedelta(seconds=10))
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work.read_text().splitlines() == [jid]
    assert J.get_job(jid)["state"] == "paused"
    assert [x["status"] for x in E.list_executions(job_id=jid)] == ["unknown"]
    assert list((cron_home / "cron/admissions").glob("*.json"))


def test_resubmit_lost_reply_then_update_paused_redial_stays_unknown_same_request_id(
    cron_home, monkeypatch, transport
):
    """#53 resubmit path: transient storage_unavailable -> recover=missing -> lost resubmit
    reply -> update_paused redial must keep one request_id and never refund the slot."""
    import json
    from cron import scheduler_authority

    ev, work = transport
    monkeypatch.setattr(scheduler_authority, "STORAGE_RETRY_DELAYS_S", (0.001,), raising=False)
    monkeypatch.setattr(scheduler_authority, "RECONNECT_BUDGET_S", 0.05, raising=False)
    monkeypatch.setattr(scheduler_authority, "RECONNECT_FIRST_DELAY_S", 0.01, raising=False)

    jid, due = make_due("every 30m", timedelta(seconds=10))
    calls = []
    dials = {"count": 0}
    real_connect = client.connect_gateway

    class ResubmitOwner:
        async def rpc(self, op, **params):
            calls.append((op, params.get("request_id")))
            if op == "cron.submit":
                if sum(1 for m, _ in calls if m == "cron.submit") == 1:
                    raise client.GatewayRPCError(
                        "storage_unavailable", 5001, {"reason": "storage_unavailable"}
                    )
                raise client.GatewayClientError(client.DISCONNECTED)
            if op == "cron.recover":
                return {"status": "missing", "result": None}
            raise AssertionError(f"unexpected rpc {op}")

    @asynccontextmanager
    async def connect():
        dials["count"] += 1
        if dials["count"] == 1:
            yield ResubmitOwner()
            return
        if not ev["markers"]:
            ev["markers"].append(_fence(cron_home))
        async with real_connect():
            pytest.fail("fenced redial must raise update_paused")

    monkeypatch.setattr(client, "connect_gateway", connect)

    S.tick(verbose=False, sync=True)

    assert [op for op, _ in calls] == ["cron.submit", "cron.recover", "cron.submit"]
    request_ids = {rid for _, rid in calls}
    assert len(request_ids) == 1
    req_id = next(iter(request_ids))
    assert req_id is not None and not work.exists()
    assert dials["count"] >= 2

    row = J.get_job(jid)
    assert row["state"] == "paused"
    assert row["next_run_at"] != due
    assert (row.get("last_fire_error") or {}).get("update_slot") is None

    exec_rows = E.list_executions(job_id=jid)
    assert len(exec_rows) == 1 and exec_rows[0]["id"] == req_id
    assert exec_rows[0]["status"] == "unknown"

    journals = list((cron_home / "cron/admissions").glob("*.json"))
    assert len(journals) == 1
    saved = json.loads(journals[0].read_text(encoding="utf-8-sig"))
    assert saved["params"]["request_id"] == req_id
    assert "update_refused" not in saved


def test_fence_does_not_replay_an_already_executed_monitor_script(cron_home, transport):
    ev, work = transport
    counter = cron_home / "monitor-runs"
    script = cron_home / "scripts" / "observe.sh"
    script.write_text(f"#!/bin/sh\necho sampled >> {counter}\necho changed\n")
    script.chmod(0o755)
    jid, _ = make_due("every 30m", timedelta(seconds=10))
    J.update_job(jid, {"monitor_script": "observe.sh"})
    S.tick(verbose=False, sync=True)
    assert ev["errors"] == [ERROR] and not work.exists()
    assert counter.read_text().splitlines() == ["sampled"]
    assert (
        J.get_job(jid)["last_status"] == "error"
        and J.get_job(jid)["repeat"]["completed"] == 6
    )
    ev["markers"][-1].unlink()
    S.tick(verbose=False, sync=True)
    assert counter.read_text().splitlines() == ["sampled"]


@pytest.mark.parametrize("intervened", ["new-owner", "edited-plan"])
def test_refusal_does_not_overwrite_a_newer_owner_or_due_date(
    cron_home, transport, intervened
):
    ev, work = transport
    jid, _ = make_due("every 30m", timedelta(seconds=10))
    future = (J._hermes_now() + timedelta(days=7)).isoformat()

    def intervene():
        jobs = J.load_jobs()
        j = next(x for x in jobs if x["id"] == jid)
        if intervened == "new-owner":
            j["fire_claim"]["by"] = "replacement"
        else:
            j["schedule"]["minutes"] = 90
        j["next_run_at"] = future
        J.save_jobs(jobs)

    ev["after_claim"] = intervene
    S.tick(verbose=False, sync=True)
    row = J.get_job(jid)
    assert (
        ev["errors"] == [ERROR] and not work.exists() and row["next_run_at"] == future
    )
    assert row["repeat"]["completed"] == 5 and row["last_status"] is None
    if intervened == "new-owner":
        assert row["fire_claim"]["by"] == "replacement"
    else:
        assert not row.get("fire_claim") and row["schedule"]["minutes"] == 90


def test_an_admitted_slot_is_not_a_new_miss_after_40_minutes(
    cron_home, monkeypatch, transport
):
    ev, work = transport
    now = [J._hermes_now()]
    monkeypatch.setattr(J, "_hermes_now", lambda: now[0])
    jid, due = make_due("every 30m", timedelta(seconds=5))
    J.update_job(jid, {"catch_up": False})
    S.tick(
        verbose=False, sync=True
    )  # on time, then refused on connect, not a missed occurrence
    for _ in range(40):
        now[0] += timedelta(minutes=1)
        S.tick(verbose=False, sync=True)
    ev["markers"][-1].unlink()
    ev["refuse"] = False
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work.read_text().splitlines() == [jid]
    assert [
        x["scheduled_instant"]
        for x in E.list_executions(job_id=jid)
        if x["status"] == "completed"
    ] == [due]


@pytest.mark.platforms("linux")
def test_detached_worker_and_waiter_do_not_book_the_refusal_twice(
    cron_home, monkeypatch
):
    import os
    from tools.process_registry import GatewayChildDispatch
    from cron import scheduler_authority

    jid, due = make_due("every 30m", timedelta(seconds=10))
    seen = []

    def dispatch(command, **kw):
        # The parent passed the scan, claimed and advanced. Its unmocked child will hit
        # ensure_gateway_runtime's actual marker gate in this disposable profile.
        assert J.get_job(jid)["fire_claim"]["by"]
        seen.append(_fence(cron_home))
        return GatewayChildDispatch("degraded", command)

    monkeypatch.setattr(
        "tools.process_registry.restart_safe_gateway_child_argv", dispatch
    )
    monkeypatch.setattr(S, "_maybe_run_worktree_maintenance", lambda: None)
    for n in (1, 2):
        S.tick(verbose=False, sync=True)
        rows = E.list_executions(job_id=jid)
        assert len(rows) == n and all(
            x["pid"] != os.getpid() and x["status"] == "cancelled" for x in rows
        ), rows
        assert (
            J.get_job(jid)["next_run_at"] == due
            and J.get_job(jid)["repeat"]["completed"] == 5
        )
        assert not list(J.OUTPUT_DIR.glob(f"{jid}/*.md"))
        seen[-1].unlink()
    work = []
    monkeypatch.setattr(S, "_launch_external_cron_worker", lambda job: False)
    monkeypatch.setattr(
        scheduler_authority,
        "run_canonical_job",
        lambda job, **kw: (
            work.append(job["id"]) or True,
            "# observation",
            "observed",
            None,
        ),
    )
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert work == [jid] and J.get_job(jid)["repeat"]["completed"] == 6


def test_retry_only_the_rollback_when_its_store_write_fails(
    cron_home, monkeypatch, transport
):
    from cron import scheduler_authority

    ev, work = transport
    # Use the real recovery loop. A refusal receipt must NOT use owner RPC to recover.
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", _real_reconcile)
    once = (J._hermes_now() + timedelta(minutes=5)).isoformat()
    jid, due = make_due(once, timedelta(seconds=10), times=1, completed=0)
    real_save = J.save_jobs
    blocked = []

    def fail_once(rows, **kw):
        if not blocked and any(
            j.get("last_fire_error", {}).get("update_slot") == due for j in rows
        ):
            blocked.append(True)
            raise OSError("injected: store temporarily read-only")
        return real_save(rows, **kw)

    ev["after_claim"] = lambda: monkeypatch.setattr(J, "save_jobs", fail_once)
    S.tick(verbose=False, sync=True)
    assert blocked == [True] and not work.exists()
    assert list((cron_home / "cron/admissions").glob("*.json"))
    S.tick(verbose=False, sync=True)  # rollback only; marker still present
    assert (
        J.get_job(jid)["repeat"]["completed"] == 0
        and J.get_job(jid)["next_run_at"] == due
    )
    assert (
        not list((cron_home / "cron/admissions").glob("*.json")) and not work.exists()
    )
    ev["markers"][-1].unlink()
    ev["refuse"] = False
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)
    assert (
        work.read_text().splitlines() == [jid]
        and J.get_job(jid)["repeat"]["completed"] == 1
    )
