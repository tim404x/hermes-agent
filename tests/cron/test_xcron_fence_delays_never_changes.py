"""extra-cron audit regression: an update fence only DELAYS agent jobs, never changes their outcome.

Differential: the same five-job store is ticked (a) with no fence, (b) under a live fence for two
ticks, then the fence lifts and it ticks twice more. While fenced: no agent submission, every agent
slot/claim/counter/retry-rung untouched, the script job fires independently exactly once. After the
lift, every booked field compared below must equal the no-fence control (held agent daily, script,
one-shot inside grace, 3 h stale catch-up slot, recurring job sitting on an unreachable-retry rung
with a repeat quota). Hermetic regardless of conftest: _payload_manifest is neutralised here.
"""
import os
import time
from datetime import timedelta

import pytest

import cron.jobs as J
import cron.scheduler as S
from cron import scheduler_authority


def _bind_cron_home(home, monkeypatch):
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    for k, v in (
        ("HERMES_DIR", home),
        ("CRON_DIR", home / "cron"),
        ("JOBS_FILE", home / "cron" / "jobs.json"),
        ("OUTPUT_DIR", home / "cron" / "output"),
    ):
        monkeypatch.setattr(J, k, v)
    monkeypatch.setattr(S, "_hermes_home", home)
    monkeypatch.setattr(S, "_sweep_mcp_orphans", lambda: None)
    monkeypatch.setattr(
        scheduler_authority, "reconcile_pending", lambda *, allow_connect=True: None
    )
    import pm.environments as PE

    monkeypatch.setattr(PE, "_payload_manifest", lambda root: None)
    S._running_job_ids.clear()
    return home


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = _bind_cron_home(tmp_path / "home", monkeypatch)
    yield home
    S._shutdown_parallel_pool()


def _due(job_id, ago, **extra):
    stored = J.load_jobs()
    slot = (J._hermes_now() - ago).replace(microsecond=0).isoformat()
    row = next(r for r in stored if r["id"] == job_id)
    row["next_run_at"] = slot
    for k, v in extra.items():
        row[k] = v(slot) if callable(v) else v
    J.save_jobs(stored)
    return slot


def _fence(home):
    from hermes_cli.update_lock import MARKER_NAME, process_create_time

    m = home / MARKER_NAME
    m.write_text(
        f"{os.getpid()}\n{int(time.time())}\nct:{process_create_time():.3f}\nrun:test\n"
    )
    return m


def _run_fence_scenario(home, monkeypatch, mode: str) -> dict:
    from cron.occurrences import get_catch_up_occurrence_count
    from cron.unreachable_retry import STATE_KEY

    with monkeypatch.context() as m:
        _bind_cron_home(home, m)
        try:
            submitted = []
            m.setattr(
                scheduler_authority,
                "run_canonical_job",
                lambda job, **kw: (submitted.append(job["id"]) or True, "# ok\n", "ok", None),
            )
            fires = home / "fires.txt"
            sc = home / "scripts" / "fire.sh"
            sc.write_text(f"#!/bin/sh\necho fired >> {fires}\necho fired\n")
            sc.chmod(0o755)
            later = (J._hermes_now() + timedelta(hours=1)).replace(microsecond=0).isoformat()
            jobs = {
                "daily": J.create_job(
                    prompt="housing scout", schedule="every 1d", name="daily", deliver="local"
                ),
                "script": J.create_job(
                    prompt=None,
                    schedule="every 1h",
                    name="script",
                    script="fire.sh",
                    no_agent=True,
                    deliver="local",
                ),
                "oneshot": J.create_job(
                    prompt="call landlord", schedule=later, name="oneshot", deliver="local"
                ),
                "catchup": J.create_job(
                    prompt="inbox digest", schedule="every 1h", name="catchup", deliver="local"
                ),
                "retry": J.create_job(
                    prompt="ml brief", schedule="every 1d", name="retry", deliver="local", repeat=3
                ),
            }
            ids = {k: v["id"] for k, v in jobs.items()}
            _due(ids["daily"], timedelta(minutes=1))
            _due(ids["script"], timedelta(minutes=1))
            _due(ids["oneshot"], timedelta(seconds=30))
            _due(ids["catchup"], timedelta(hours=3))
            expr = J.load_jobs()[[j["id"] for j in J.load_jobs()].index(ids["retry"])][
                "schedule"
            ].get("expr")
            _due(
                ids["retry"],
                timedelta(minutes=2),
                repeat={"times": 3, "completed": 1},
                **{STATE_KEY: lambda slot: {"attempt": 1, "at": slot, "expr": expr}},
            )
            before = {j["id"]: dict(j) for j in J.load_jobs()}

            if mode == "fenced":
                marker = _fence(home)
                S.tick(verbose=False, sync=True)
                S.tick(verbose=False, sync=True)
                held = {j["id"]: j for j in J.load_jobs()}
                assert submitted == [], "no agent worker may run into the fence"
                assert fires.read_text().count("fired") == 1, (
                    "the script job fires independently, once"
                )
                for name in ("daily", "oneshot", "catchup", "retry"):
                    b, h = before[ids[name]], held[ids[name]]
                    for f in (
                        "next_run_at",
                        "repeat",
                        STATE_KEY,
                        "failure_streak",
                        "last_run_at",
                        "last_status",
                    ):
                        assert h.get(f) == b.get(f), (
                            f"{name}.{f} changed while held: {b.get(f)!r} -> {h.get(f)!r}"
                        )
                    assert h.get("run_claim") is None and "pending_slot" not in h, (
                        f"{name}: claimed while held"
                    )
                assert get_catch_up_occurrence_count() == 0, (
                    "no catch-up booked for a fire that did not happen"
                )
                marker.unlink()

            S.tick(verbose=False, sync=True)
            S.tick(verbose=False, sync=True)
            final = {j["id"]: j for j in J.load_jobs()}
            rev = {v: k for k, v in ids.items()}
            return {
                "submitted": sorted(rev[i] for i in submitted),
                "script_fires": fires.read_text().count("fired") if fires.exists() else 0,
                "catch_up_count": get_catch_up_occurrence_count(),
                "jobs": {
                    name: None
                    if ids[name] not in final
                    else {
                        "last_status": final[ids[name]].get("last_status"),
                        "repeat": final[ids[name]].get("repeat"),
                        "retry_state": bool(final[ids[name]].get(STATE_KEY)),
                        "failure_streak": final[ids[name]].get("failure_streak"),
                        "future": J._parse_aware(final[ids[name]]["next_run_at"]) > J._hermes_now()
                        if final[ids[name]].get("next_run_at")
                        else None,
                        "fire_error": "last_fire_error" in final[ids[name]],
                    }
                    for name in ids
                },
            }
        finally:
            S._shutdown_parallel_pool()


@pytest.mark.platforms("linux")
def test_fence_outcome_equals_control(tmp_path, monkeypatch):
    control = _run_fence_scenario(tmp_path / "control", monkeypatch, "control")
    fenced = _run_fence_scenario(tmp_path / "fenced", monkeypatch, "fenced")
    assert fenced == control, f"fence changed the outcome:\n{fenced}\nvs\n{control}"
    assert control["submitted"] == ["catchup", "daily", "oneshot", "retry"], control
