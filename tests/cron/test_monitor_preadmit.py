"""apexfix: monitor gate runs before admission (cron/monitor_preadmit.py).

An unchanged scheduled monitor tick must return the owner's own no_change result WITHOUT an
admission (no session row, no policy receipt); every other tick (changed, first run, source failure,
manual run, kill switch) must take today's admission path unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tests.cron.conftest import owner_execution_runner
from tests.cron.test_monitor_kind import _install_agent_stubs, _make_monitor_job, _write_script

FIXED_NOW = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CRON_MONITOR_PREADMIT", raising=False)
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.monitor
    importlib.reload(cron.monitor)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    import cron.monitor_preadmit
    importlib.reload(cron.monitor_preadmit)
    return home


@pytest.fixture
def admissions(monkeypatch):
    """The production owner execution seam, counting admissions (each one = a session + a receipt)."""
    run = owner_execution_runner()
    calls = []

    def counted(job, **kwargs):
        calls.append((job["id"], kwargs.get("extra_prompt")))
        return run(job, **kwargs)

    monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", counted)
    return calls


def _counting_script(home, body_line: str) -> None:
    """A monitor script that records each execution, so double runs are visible."""
    _write_script(home, "mon.sh", f"echo run >> '{home}/mon_runs.log'\n{body_line}\n")


def _runs(home) -> int:
    path = home / "mon_runs.log"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _ledger(home) -> list[dict]:
    path = home / "cron" / "monitor_gate.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _scheduled(job: dict) -> dict:
    """The job dict as the ticker hands it to run_job for a scheduled occurrence."""
    return dict(job, _scheduled_instant=FIXED_NOW.isoformat(), execution_id="exec-" + job["id"])


def _freeze_now(monkeypatch):
    import cron.scheduler as sched
    monkeypatch.setattr(sched, "_hermes_now", lambda: FIXED_NOW)


def _baseline(home, monkeypatch, observed):
    """First run (agent runs, hash stored) through the old path, as in production."""
    from cron.jobs import get_job
    from cron.scheduler import run_job

    _counting_script(home, "echo 'state A'")
    job = _make_monitor_job(home, "true\n")
    _counting_script(home, "echo 'state A'")
    _install_agent_stubs(monkeypatch, observed)
    run_job(job)  # no _scheduled_instant: a manual-like first run, always admitted
    assert observed["agent_runs"] == 1
    # The owner re-checked just now (revalidation clock), as after any admitted scheduled tick.
    from cron.monitor_preadmit import _update_state
    _update_state(job["id"], last_admit_at=__import__("time").time())
    return get_job(job["id"])


def test_unchanged_tick_skips_admission_with_the_owners_exact_result(hermes_env, monkeypatch, admissions):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    _freeze_now(monkeypatch)
    state_before = get_job(job["id"])["monitor_state"]
    snapshot = hermes_env / "cron" / "output" / job["id"] / "monitor_last_output.txt"
    snap_before = snapshot.read_text()
    admitted_before, runs_before = len(admissions), _runs(hermes_env)

    new = run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == admitted_before  # NO admission -> no session, no receipt
    assert _runs(hermes_env) == runs_before + 1  # the source ran once, in the firer
    assert observed["agent_runs"] == 1
    assert get_job(job["id"])["monitor_state"] == state_before  # nothing persisted
    assert snapshot.read_text() == snap_before

    # The old path (kill switch) for the same tick: admitted, the owner returns no_change.
    monkeypatch.setenv("HERMES_CRON_MONITOR_PREADMIT", "0")
    old = run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == admitted_before + 1
    assert new == old  # byte-identical (success, doc, SILENT_MARKER, None)
    assert new[2] == "[SILENT]" and new[0] is True and new[3] is None and "no_change" in new[1]

    entries = _ledger(hermes_env)
    assert [(e["decision"], e["reason"]) for e in entries if e["job_id"] == job["id"]][-1] == ("skip", "unchanged")
    assert entries[-1]["hash"] == state_before["last_output_hash"][:12]
    assert isinstance(entries[-1]["elapsed_ms"], float)


def test_changed_tick_is_admitted_once_and_the_agent_runs(hermes_env, monkeypatch, admissions):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    old_hash = get_job(job["id"])["monitor_state"]["last_output_hash"]
    _counting_script(hermes_env, "echo 'state B'")
    admitted_before, runs_before = len(admissions), _runs(hermes_env)

    ok, _doc, final, err = run_job(_scheduled(get_job(job["id"])))
    assert ok is True and err is None and final == "agent done"
    assert len(admissions) == admitted_before + 1
    assert observed["agent_runs"] == 2
    assert "+state B" in observed["prompts"][-1]
    new_hash = get_job(job["id"])["monitor_state"]["last_output_hash"]
    assert new_hash != old_hash  # persisted once, by the owner
    assert _runs(hermes_env) == runs_before + 2  # firer peek + owner check
    assert _ledger(hermes_env)[-1]["decision"] == "admit"
    assert _ledger(hermes_env)[-1]["reason"] == "changed"
    assert _ledger(hermes_env)[-1]["hash"] == new_hash[:12]

    # And the next unchanged tick is skipped against the hash the owner stored.
    run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == admitted_before + 1
    assert _ledger(hermes_env)[-1]["decision"] == "skip"


def test_revalidation_admits_at_least_every_max_skip_window(hermes_env, monkeypatch, admissions):
    import time as _time

    from cron.jobs import get_job
    from cron.monitor_preadmit import _update_state
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    _update_state(job["id"], last_admit_at=_time.time() - 31 * 60)
    before = len(admissions)
    result = run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == before + 1 and result[2] == "[SILENT]"  # owner re-checked: still no_change
    assert _ledger(hermes_env)[-1]["reason"] == "revalidate"
    run_job(_scheduled(get_job(job["id"])))  # clock reset by the admission: skipped again
    assert len(admissions) == before + 1
    assert _ledger(hermes_env)[-1]["decision"] == "skip"


def test_divergence_latches_the_gate_off_for_that_job(hermes_env, monkeypatch, admissions):
    """If the owner sees a change the firer's peek did not, the job always admits from then on."""
    import time as _time

    import cron.monitor as mon
    from cron.jobs import get_job
    from cron.monitor_preadmit import _load_state, _update_state
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    _counting_script(hermes_env, "echo 'state B'")  # the real source changed...
    real_peek = mon.peek_monitor
    stored = get_job(job["id"])["monitor_state"]["last_output_hash"]
    # ...but the firer's environment keeps reporting the old output (the reviewer's env-mismatch case).
    monkeypatch.setattr(mon, "peek_monitor", lambda j: mon.MonitorPeek(ok=True, unchanged=True, output_hash=stored))
    before = len(admissions)
    run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == before  # skipped (within the window): the bounded-delay case
    _update_state(job["id"], last_admit_at=_time.time() - 31 * 60)
    run_job(_scheduled(get_job(job["id"])))  # revalidation: the owner runs the agent on the change
    assert len(admissions) == before + 1 and observed["agent_runs"] == 2
    assert _load_state()[job["id"]]["disabled"] == "divergence"
    run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == before + 2  # latched: admits every tick, like the old path
    assert _ledger(hermes_env)[-1]["reason"] == "disabled:divergence"
    monkeypatch.setattr(mon, "peek_monitor", real_peek)


def test_first_run_is_admitted(hermes_env, monkeypatch, admissions):
    from cron.scheduler import run_job

    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed)
    job = _make_monitor_job(hermes_env, "echo 'state A'\n")
    run_job(_scheduled(job))
    assert len(admissions) == 1 and observed["agent_runs"] == 1
    assert _ledger(hermes_env)[-1]["reason"] == "first_run"


def test_source_failure_takes_todays_alert_path(hermes_env, monkeypatch, admissions):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    stored = get_job(job["id"])["monitor_state"]["last_output_hash"]
    _write_script(hermes_env, "mon.sh", "echo boom >&2\nexit 3\n")
    before = len(admissions)
    ok, doc, alert, err = run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == before + 1  # the owner decides and alerts, as today
    assert ok is False and err and "monitor source failed" in doc and "source failed" in alert
    assert get_job(job["id"])["monitor_state"]["last_output_hash"] == stored
    assert _ledger(hermes_env)[-1]["decision"] == "error"


def test_kill_switch_restores_the_old_path(hermes_env, monkeypatch, admissions):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    monkeypatch.setenv("HERMES_CRON_MONITOR_PREADMIT", "0")
    lines, before, runs = len(_ledger(hermes_env)), len(admissions), _runs(hermes_env)
    result = run_job(_scheduled(get_job(job["id"])))
    assert len(admissions) == before + 1 and result[2] == "[SILENT]"
    assert _runs(hermes_env) == runs + 1  # only the owner ran the source
    assert len(_ledger(hermes_env)) == lines  # no decision made, none recorded


@pytest.mark.parametrize("variant", ["extra_prompt", "no_instant", "extra_fire"])
def test_manual_runs_always_admit(hermes_env, monkeypatch, admissions, variant):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    observed: dict = {}
    job = _baseline(hermes_env, monkeypatch, observed)
    fire = _scheduled(get_job(job["id"]))
    kwargs = {}
    if variant == "extra_prompt":
        kwargs["extra_prompt"] = "look again"
    elif variant == "no_instant":
        fire["_scheduled_instant"] = None
    else:
        fire["fire_claim"] = {"at": FIXED_NOW.isoformat(), "by": "x", "extra_slot": FIXED_NOW.isoformat()}
    before, runs = len(admissions), _runs(hermes_env)
    result = run_job(fire, **kwargs)
    assert len(admissions) == before + 1
    assert _runs(hermes_env) == runs + 1  # the firer did not run the source
    assert result[2] == "[SILENT]"  # the owner's own monitor gate decided, exactly as today
    assert _ledger(hermes_env)[-1]["decision"] == "admit"
    assert _ledger(hermes_env)[-1]["reason"].startswith(("manual_", "not_a_scheduled"))


def test_non_monitor_and_no_agent_jobs_are_untouched(hermes_env, monkeypatch, admissions):
    from cron.monitor_preadmit import preadmit_monitor_gate

    assert preadmit_monitor_gate({"id": "plain", "prompt": "x", "_scheduled_instant": "t"}) is None
    assert preadmit_monitor_gate({"id": "na", "no_agent": True, "monitor_script": "m.sh",
                                  "_scheduled_instant": "t"}) is None
    assert _ledger(hermes_env) == []


def test_ledger_rotates_and_keeps_two(hermes_env, monkeypatch):
    import cron.monitor_preadmit as mp

    monkeypatch.setattr(mp, "LEDGER_MAX_BYTES", 200)
    other = hermes_env / "cron" / "jobs.json.bak-keep"
    other.write_text("keep me")
    for i in range(40):
        mp.record_decision(f"job{i}", "skip", reason="unchanged", output_hash="ab" * 32, elapsed_ms=1.0)
    names = sorted(p.name for p in (hermes_env / "cron").iterdir() if p.name.startswith("monitor_gate"))
    assert names == ["monitor_gate.jsonl", "monitor_gate.jsonl.1", "monitor_gate.jsonl.2"]
    assert other.read_text() == "keep me"
    assert all(p.stat().st_size < 400 for p in (hermes_env / "cron").glob("monitor_gate.jsonl*"))
