"""An update fence during a due tick leaves a one-shot and a catch-up slot due, then fires each once.

``hold_agent_jobs_while_update_fenced`` (#27) ran AFTER the due scan, and the scan had already
acted on the slot: a recurring job past its grace was fast-forwarded to its next future slot (the
catch-up), its ``pending_slot`` stamp was dropped by the hold, and the ``catch_up_occurrences``
counter plus the ``missed`` ledger row were booked for a fire that never happened. When the fence
lifted the job was no longer due: the catch-up run was lost. The fence is now read once per tick,
before the scan, and the scan hands held agent jobs over untouched.
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


def _set_next_run(job_id: str, ago: timedelta) -> str:
    stored = J.load_jobs()
    slot = (J._hermes_now() - ago).replace(microsecond=0).isoformat()
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
def test_update_fence_leaves_oneshot_and_catch_up_slots_due_then_fires_each_once(cron_home, monkeypatch):
    from cron.occurrences import get_catch_up_occurrence_count

    submitted = []

    def fake_canonical(job, **_kw):
        submitted.append(job["id"])
        return True, "# ok\n", "ok", None

    monkeypatch.setattr(scheduler_authority, "run_canonical_job", fake_canonical)
    later = (J._hermes_now() + timedelta(hours=1)).replace(microsecond=0).isoformat()
    once = J.create_job(prompt="call the landlord", schedule=later, name="reminder", deliver="local")
    hourly = J.create_job(prompt="inbox digest", schedule="every 1h", name="hourly", deliver="local")
    once_slot = _set_next_run(once["id"], timedelta(seconds=30))  # due, inside its 120 s grace
    # Three hours stale (gateway away): past the 30 min grace, so this tick is its catch-up slot.
    catch_up_slot = _set_next_run(hourly["id"], timedelta(hours=3))
    marker = _fence(cron_home)

    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)  # a second held tick in the same window

    stored = {j["id"]: j for j in J.load_jobs()}
    assert submitted == [], "no agent worker may run into the fence"
    for job_id, slot in ((once["id"], once_slot), (hourly["id"], catch_up_slot)):
        held = stored[job_id]
        assert held["next_run_at"] == slot, f"{held['name']}: the slot stays due (not consumed)"
        assert held["last_run_at"] is None and held["last_status"] is None, f"{held['name']}: held, not failed"
        assert held.get("run_claim") is None and "pending_slot" not in held
        assert "update" in held["last_fire_error"]["detail"]
    assert get_catch_up_occurrence_count() == 0, "no catch-up is counted for a fire that did not happen"

    marker.unlink()
    S.tick(verbose=False, sync=True)
    S.tick(verbose=False, sync=True)  # the slots were consumed by the first fire: nothing re-fires

    assert sorted(submitted) == sorted([once["id"], hourly["id"]]), "each held slot fires exactly once"
    stored = {j["id"]: j for j in J.load_jobs()}
    fired = stored[hourly["id"]]
    assert fired["last_status"] == "ok" and "last_fire_error" not in fired
    assert J._parse_aware(fired["next_run_at"]) > J._hermes_now(), "re-anchored past the catch-up"
    assert get_catch_up_occurrence_count() == 1, "one catch-up, counted once"
    reminder = stored.get(once["id"])
    assert reminder is None or reminder["last_status"] == "ok", "the one-shot completed, not retired as missed"
