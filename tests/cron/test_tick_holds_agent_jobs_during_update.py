"""While ``hermes update`` holds its fence, the ticker holds agent jobs instead of failing them.

``hermes update`` waits for the gateway to go idle (up to ``agent.restart_after_turn_timeout``)
while the gateway keeps serving and its in-process ticker keeps dispatching. Every agent job's
worker then reaches ``connect_gateway`` -> ``ensure_gateway_runtime``, which answers a fenced install
with ``draining (update_paused)``, and the run was booked FAILED with its slot consumed: a
34-minute morning update failed every agent cron due in it, and a daily job lost its day.

Held instead: no run recorded, the due instant untouched, script jobs still fire, and once the
fence lifts the same tick dispatches the held job.
"""
import os
import time
from datetime import timedelta

import pytest

import cron.jobs as J
import cron.scheduler as S
from cron import scheduler_authority


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(scheduler_authority, "reconcile_pending", lambda *, allow_connect=True: None)
    S._running_job_ids.clear()
    yield home
    S._shutdown_parallel_pool()


def _make_due(job_id: str) -> str:
    stored = J.load_jobs()
    slot = (J._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    next(r for r in stored if r["id"] == job_id)["next_run_at"] = slot
    J.save_jobs(stored)
    return slot


def _fence(home):
    """A LIVE update claim (this test process, at its own incarnation), as `hermes update` writes it."""
    from hermes_cli.update_lock import MARKER_NAME, process_create_time
    marker = home / MARKER_NAME
    marker.write_text(f"{os.getpid()}\n{int(time.time())}\nct:{process_create_time():.3f}\nrun:test\n",
                      encoding="utf-8")
    return marker


@pytest.mark.platforms("linux")
def test_update_fence_holds_agent_jobs_and_fires_them_once_it_lifts(cron_home, monkeypatch, caplog):
    submitted = []

    def fake_canonical(job, **_kw):
        submitted.append(job["id"])
        return True, "# ok\n", "ok", None

    monkeypatch.setattr(scheduler_authority, "run_canonical_job", fake_canonical)
    counter = cron_home / "fires.txt"
    script = cron_home / "scripts" / "fire.sh"
    script.write_text(f"#!/bin/sh\necho fired >> {counter}\necho fired\n", encoding="utf-8")
    script.chmod(0o755)
    agent = J.create_job(prompt="housing scout", schedule="every 1d", name="daily", deliver="local")
    side = J.create_job(prompt=None, schedule="every 1h", name="script", script="fire.sh",
                        no_agent=True, deliver="local")
    slot = _make_due(agent["id"])
    _make_due(side["id"])
    marker = _fence(cron_home)

    with caplog.at_level("WARNING", logger="cron.scheduler_gateway_gate"):
        S.tick(verbose=False, sync=True)
        S.tick(verbose=False, sync=True)  # a second held tick in the same window

    stored = {j["id"]: j for j in J.load_jobs()}
    held = stored[agent["id"]]
    assert submitted == [], "no agent worker may run into the fence"
    assert held["last_run_at"] is None and held["last_status"] is None, "held, not failed"
    assert held["next_run_at"] == slot, "the slot stays due"
    assert "pending_slot" not in held
    assert "update" in held["last_fire_error"]["detail"]
    assert counter.read_text(encoding="utf-8").count("fired") == 1, "script jobs still fire"
    assert len([r for r in caplog.records if "held" in r.getMessage()]) == 1, "one warning per window"
    from cron import dispatch_hold
    assert dispatch_hold.read(cron_home / "cron") is not None, "the hold is stamped so a one-shot keeps its grace"

    marker.unlink()
    S.tick(verbose=False, sync=True)

    stored = {j["id"]: j for j in J.load_jobs()}
    assert submitted == [agent["id"]], "the held job fires once the fence lifts"
    assert stored[agent["id"]]["last_status"] == "ok"
    assert "last_fire_error" not in stored[agent["id"]]
