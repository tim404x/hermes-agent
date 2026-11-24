"""Monitor gate before admission: an unchanged monitor tick costs no session.

# apexfix: monitor gate runs before admission

An agent-backed cron fire goes firer ``run_job`` -> ``run_canonical_job`` -> gateway ``cron.submit``,
which reserves a session row plus a ~40 KB ``gateway.local_policy.v1:<session>`` receipt; only then does
the owner run ``_apply_monitor_gate`` and, for an unchanged source, return a silent ``no_change``
result. A monitor job firing every minute therefore left one empty session and one receipt per tick.

Here the FIRER runs the same monitor source first and compares its hash with the stored
``monitor_state.last_output_hash`` without persisting anything (``cron.monitor.peek_monitor``):

- unchanged (source ok): return exactly the owner's ``no_change`` result
  (``scheduler._monitor_no_change_result``). The caller books it like any other result, so ledger,
  repeat counters, ``last_run_at``, output doc and (no) delivery are those of today's no_change tick;
  only the admission (session row, receipt, admission journal) never happens.
- changed, first run, source failure, or anything this module cannot decide: return None, and the fire
  takes today's admission path untouched. The owner re-runs the monitor, persists the hash and runs the
  agent, or raises today's source-failure alert.

Only plain scheduled occurrences are peeked. A manual run (``cronjob run`` / ``hermes cron run`` /
dashboard Run, i.e. an extra fire, a fire without a scheduled instant, or one carrying ``extra_prompt``)
always admits, so its behaviour is unchanged.

Safety (the firer may run in a restart-safe external worker whose environment is not the owner's):

- revalidation: a job is admitted at least every ``HERMES_CRON_MONITOR_PREADMIT_MAX_SKIP_MIN`` minutes
  (default 30) even when the peek says unchanged, so a wrong "unchanged" can delay a real run by at most
  that long, never drop it;
- divergence latch: after every admission the firer compares the owner's verdict (did the stored hash move?)
  with its own peek. The FIRST time the owner sees a change the peek did not, the job goes on probation for
  ``PROBATION_S``: every tick admits (the owner decides, nothing is delayed) while the firer keeps peeking and
  comparing. A SECOND divergence within ``DIVERGENCE_WINDOW_S`` switches the gate off for that job for good
  (``monitor_gate_state.json``, WARNING logged); every later tick of that job admits as before. One divergence
  alone is usually a flapping source (one transient output between the peek and the owner's run seconds later;
  seen on a live Slack/Gmail monitor), which must not cost the job its gate. A peek that is really blind keeps
  diverging on the next real change, and during probation that change is admitted, not delayed.
  # apexfix: divergence latch needs a second miss (probation after the first)

Every decision appends one JSON line to ``<cron store>/monitor_gate.jsonl`` (rotated at 5 MB, 2 kept)
and logs one INFO line. Kill switch: ``HERMES_CRON_MONITOR_PREADMIT=0`` restores the old path exactly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Optional

logger = logging.getLogger(__name__)

KILL_SWITCH_ENV = "HERMES_CRON_MONITOR_PREADMIT"
LEDGER_NAME = "monitor_gate.jsonl"
LEDGER_MAX_BYTES = 5 * 1024 * 1024
LEDGER_KEEP = 2  # monitor_gate.jsonl.1 .. .2 (only this module's own files are ever rotated)

_ledger_lock = threading.Lock()


MAX_SKIP_ENV = "HERMES_CRON_MONITOR_PREADMIT_MAX_SKIP_MIN"
DEFAULT_MAX_SKIP_MIN = 30.0
STATE_NAME = "monitor_gate_state.json"
DIVERGENCE_LATCH_COUNT = 2  # divergences within DIVERGENCE_WINDOW_S that switch a job's gate off for good
DIVERGENCE_WINDOW_S = 24 * 3600.0
PROBATION_S = 6 * 3600.0  # after one divergence: admit every tick (owner decides) and keep comparing


def enabled() -> bool:
    return os.environ.get(KILL_SWITCH_ENV, "").strip() != "0"


def _max_skip_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(MAX_SKIP_ENV, DEFAULT_MAX_SKIP_MIN))) * 60
    except ValueError:
        return DEFAULT_MAX_SKIP_MIN * 60


def _state_path():
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / STATE_NAME


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _update_state(job_id: str, **fields) -> None:
    """Best effort. A lost update (two workers at once) only means one extra admission."""
    try:
        from utils import atomic_json_write

        with _ledger_lock:
            state = _load_state()
            entry = state.get(job_id) if isinstance(state.get(job_id), dict) else {}
            state[job_id] = {**entry, **fields}
            atomic_json_write(_state_path(), state)
    except Exception:
        logger.debug("monitor pre-admission state write failed", exc_info=True)


def _ledger_path():
    from cron.jobs import _current_cron_store

    return _current_cron_store().cron_dir / LEDGER_NAME


def _rotate(path) -> None:
    """``x.jsonl`` -> ``x.jsonl.1`` -> ``x.jsonl.2``; the oldest rotation of THIS ledger is replaced."""
    for i in range(LEDGER_KEEP, 0, -1):
        src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{i}"))


def record_decision(job_id: str, decision: str, *, reason: str, output_hash: Optional[str] = None,
                    elapsed_ms: Optional[float] = None) -> None:
    """One ledger line + one INFO line per firer decision (skip | admit | error). Never raises."""
    entry = {"ts": time.time(), "job_id": job_id, "decision": decision, "reason": reason,
             "hash": (output_hash or "")[:12] or None,
             "elapsed_ms": None if elapsed_ms is None else round(elapsed_ms, 1)}
    logger.info("Job '%s': monitor pre-admission %s (%s, hash=%s, %s ms)", job_id, decision, reason,
                entry["hash"], entry["elapsed_ms"])
    try:
        path = _ledger_path()
        with _ledger_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size >= LEDGER_MAX_BYTES:
                    _rotate(path)
            except FileNotFoundError:
                pass
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        logger.debug("monitor pre-admission ledger write failed", exc_info=True)


@contextmanager
def _owner_like_scope():
    """The profile runtime scope the owner runs the monitor under (``session_cron.execute`` binds
    ``_profile_runtime_scope(profile_dir)``): same home, secrets and terminal policy, so the source
    sees the environment it sees on the owner."""
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home

    with _profile_runtime_scope(get_hermes_home()):
        yield


def _manual_reason(job: dict, extra_prompt: Optional[str]) -> Optional[str]:
    from cron import extra_fire

    if extra_prompt is not None:
        return "manual_extra_prompt"
    if extra_fire.is_extra(job):
        return "manual_extra_fire"
    if not job.get("_scheduled_instant"):
        # A run-now when the slot is already due, a catch-up or a provider fire without an occurrence
        # identity: not provably a plain scheduled tick, so it keeps the admission path.
        return "not_a_scheduled_occurrence"
    return None


class Admission:
    """What the firer saw before an admission it let through, for the divergence latch."""

    __slots__ = ("job_id", "stored_hash", "peek_unchanged")

    def __init__(self, job_id: str, stored_hash: Optional[str], peek_unchanged: bool) -> None:
        self.job_id, self.stored_hash, self.peek_unchanged = job_id, stored_hash, peek_unchanged

    def after(self) -> None:
        """Called once the admitted run finished: did the owner agree with the peek? Never raises."""
        try:
            from cron.jobs import get_job

            fresh = get_job(self.job_id) or {}
            raw = fresh.get("monitor_state")
            owner_hash = raw.get("last_output_hash") if isinstance(raw, dict) else None
            owner_changed = owner_hash != self.stored_hash
            now = time.time()
            fields = {"last_admit_at": now}
            if self.peek_unchanged and owner_changed:
                # The owner saw a change the firer's peek did not. Once can be a flapping source; twice within
                # the window means the peek cannot be trusted for this job: switch its gate off for good.
                entry = _load_state().get(self.job_id)
                prior = entry.get("divergences") if isinstance(entry, dict) else None
                recent = [t for t in (prior if isinstance(prior, list) else [])
                          if isinstance(t, (int, float)) and now - t < DIVERGENCE_WINDOW_S]
                recent.append(now)
                fields["divergences"] = recent[-(DIVERGENCE_LATCH_COUNT * 2):]
                if len(recent) >= DIVERGENCE_LATCH_COUNT:
                    logger.warning("Job '%s': monitor pre-admission peek said unchanged but the owner saw a change "
                                   "(%d times in 24 h); pre-admission gate disabled for this job",
                                   self.job_id, len(recent))
                    record_decision(self.job_id, "admit", reason="divergence_latched", output_hash=owner_hash)
                    fields["disabled"] = "divergence"
                else:
                    logger.warning("Job '%s': monitor pre-admission peek said unchanged but the owner saw a change "
                                   "(1st in 24 h, a flapping source?); every tick admits for the next %d min "
                                   "(probation), a second miss disables the gate for this job",
                                   self.job_id, int(PROBATION_S // 60))
                    record_decision(self.job_id, "admit", reason="divergence_observed", output_hash=owner_hash)
                    fields["probation_until"] = now + PROBATION_S
            _update_state(self.job_id, **fields)
        except Exception:
            logger.debug("monitor pre-admission follow-up failed", exc_info=True)


def preadmit_monitor_gate(job: dict, *, extra_prompt: Optional[str] = None,
                          cancel_event: Any = None) -> Optional[tuple]:
    """The owner's ``no_change`` result for an unchanged scheduled monitor tick, else None (admit)."""
    result, _admission = preadmit_monitor_decision(job, extra_prompt=extra_prompt, cancel_event=cancel_event)
    return result


def preadmit_monitor_decision(job: dict, *, extra_prompt: Optional[str] = None,
                              cancel_event: Any = None) -> tuple[Optional[tuple], Optional[Admission]]:
    """``(no_change result, None)`` to skip; ``(None, Admission | None)`` to admit (call ``after()``)."""
    if not enabled() or job.get("no_agent"):
        return None, None
    from cron.monitor import job_has_monitor

    if not job_has_monitor(job):
        return None, None
    job_id = str(job.get("id") or "")
    state = _load_state().get(job_id)
    state = state if isinstance(state, dict) else {}
    if state.get("disabled"):
        record_decision(job_id, "admit", reason=f"disabled:{state['disabled']}")
        return None, None
    manual = _manual_reason(job, extra_prompt)
    if manual is not None:
        record_decision(job_id, "admit", reason=manual)
        return None, None
    if cancel_event is not None and cancel_event.is_set():
        record_decision(job_id, "admit", reason="cancel_requested")
        return None, None
    started = time.monotonic()
    try:
        # The gates the owner's ``_prepare_job_prompt`` runs BEFORE the monitor: if one of them would
        # end the run differently, the owner must still be the one to say so.
        from hermes_cli.config import require_parseable_user_config

        require_parseable_user_config()
        from cron.jobs import get_job, job_payload_is_empty

        # The owner freezes ``get_job(id)`` at admission and compares against ITS monitor_state.
        fresh = get_job(job_id)
        if fresh is None or fresh.get("no_agent") or not job_has_monitor(fresh) or job_payload_is_empty(fresh):
            record_decision(job_id, "admit", reason="owner_decides")
            return None, None
        from cron.monitor import peek_monitor

        with _owner_like_scope():
            job["_monitor_source_started"] = True  # this user source may have side effects, even on error
            peek = peek_monitor(fresh)
    except Exception as exc:
        record_decision(job_id, "admit", reason=f"peek_unavailable:{type(exc).__name__}",
                        elapsed_ms=(time.monotonic() - started) * 1000)
        return None, None
    elapsed = (time.monotonic() - started) * 1000
    raw_state = fresh.get("monitor_state")
    stored = raw_state.get("last_output_hash") if isinstance(raw_state, dict) else None
    if not peek.ok:
        # Source failure is the owner's to alert on, exactly as today.
        record_decision(job_id, "error", reason="source_failed", elapsed_ms=elapsed)
        return None, Admission(job_id, stored, False)
    if not peek.unchanged:
        record_decision(job_id, "admit", reason="first_run" if peek.first_run else "changed",
                        output_hash=peek.output_hash, elapsed_ms=elapsed)
        return None, Admission(job_id, stored, False)
    probation_until = state.get("probation_until")
    if isinstance(probation_until, (int, float)) and time.time() < probation_until:
        # After a first divergence: the owner decides every tick (nothing delayed) and the comparison keeps
        # running, so a peek that is really blind is caught on the next real change.
        record_decision(job_id, "admit", reason="probation", output_hash=peek.output_hash, elapsed_ms=elapsed)
        return None, Admission(job_id, stored, True)
    last_admit = state.get("last_admit_at")
    if not isinstance(last_admit, (int, float)) or time.time() - last_admit >= _max_skip_seconds():
        # Bounded trust: the owner re-checks this job at least every MAX_SKIP minutes.
        record_decision(job_id, "admit", reason="revalidate", output_hash=peek.output_hash, elapsed_ms=elapsed)
        return None, Admission(job_id, stored, True)
    record_decision(job_id, "skip", reason="unchanged", output_hash=peek.output_hash, elapsed_ms=elapsed)
    from cron.scheduler import _monitor_no_change_result

    job_name = str(fresh.get("name") or fresh.get("prompt") or job_id or "cron job")
    return _monitor_no_change_result(job_id, job_name), None
