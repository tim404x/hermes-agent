"""Provisional run outcomes that receipt recovery later replaces.

A restart-safe worker that loses the gateway mid-observation gives up as ``unknown``; its waiter
must still retire the fire claim and count the fire (``mark_job_run(provisional=True)``), but the
owner may have finished the run, and receipt recovery (``cron.scheduler_authority.reconcile_pending``)
then books the real outcome for the same execution. That second mark replaces the provisional one
instead of counting the fire again: before this, a job limited to 2 runs completed after its first
fire, and a run that succeeded still bumped ``failure_streak`` and left a ``last_failure`` stamp.

Entries live on the job record under ``provisional_runs`` keyed by execution id, and only while the
fire's admission journal exists (only journal recovery can replace them).
"""
import copy
from typing import Any, Optional

KEY = "provisional_runs"

# Outcome state a provisional mark overwrites; its replacement restores it before booking the real
# outcome, so the streak, the sticky failure stamp and the re-run ladders move once per fire.
_RESTORED = ("failure_streak", "last_failure", "unreachable_retry",
             "quota_hold_until", "quota_hold_cron_expr")
# Diagnostics only a success clears: one written after the provisional mark is newer than this fire.
_DIAGNOSTICS = ("preflight_alerted", "last_fire_error")
# Fields a newer fire may own by the time recovery replaces an older fire's outcome.
_NEWER_FIRE_FIELDS = ("fire_claim", "run_claim", "pending_slot",
                      "manual_run_at", "manual_run_restore_at", "manual_run_prompt")
_ABSENT = "__absent__"


def snapshot(job: dict[str, Any]) -> dict[str, Any]:
    """What a provisional outcome overwrites and its replacement must restore (stamps redacted:
    this copy is stored beside the job, out of reach of the save-time stamp scrub)."""
    from cron.jobs_error_text import scrub_retained_stamps
    taken = {field: copy.deepcopy(job.get(field, _ABSENT)) for field in _RESTORED + _DIAGNOSTICS}
    scrub_retained_stamps(taken)
    return taken


def _prune(job: dict[str, Any]) -> dict[str, Any]:
    from cron.scheduler_authority import replayable_completions
    runs = job.get(KEY)
    if not isinstance(runs, dict):
        runs = {}
    live = set(replayable_completions(job.get("id"), list(runs)))
    runs = {eid: entry for eid, entry in runs.items() if eid in live and isinstance(entry, dict)}
    if runs:
        job[KEY] = runs
    else:
        job.pop(KEY, None)
    return runs


def remember(job: dict[str, Any], execution_id: str, before: dict[str, Any]) -> None:
    """Record that *execution_id*'s just-written outcome is provisional (after ``_record_run_outcome``).
    Nothing is kept for a fire without an admission journal: no recovery will ever replace it."""
    from cron.scheduler_authority import journal_path
    runs = _prune(job)
    if not journal_path(job.get("id"), execution_id).exists():
        return
    after = {field: copy.deepcopy(job.get(field, _ABSENT)) for field in _DIAGNOSTICS}
    runs[str(execution_id)] = {"before": before, "after": after, "at": job.get("last_run_at")}
    job[KEY] = runs


def take(job: dict[str, Any], execution_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Pop the provisional entry this canonical mark replaces, or None when there is none."""
    runs = job.get(KEY)
    if execution_id is None or not isinstance(runs, dict):
        return None
    entry = runs.pop(str(execution_id), None)
    _prune(job)
    return entry if isinstance(entry, dict) and isinstance(entry.get("before"), dict) else None


def _put(job: dict[str, Any], field: str, value: Any) -> None:
    if value == _ABSENT:
        job.pop(field, None)
    else:
        job[field] = copy.deepcopy(value)


def replace_outcome(job: dict[str, Any], entry: dict[str, Any], record) -> bool:
    """Swap the provisional outcome for the real one written by *record* (``_record_run_outcome``).

    The fire was already counted and its schedule advanced, so nothing else moves. False when a
    run was booked since the provisional mark: that run is newer than this fire, so its outcome
    and schedule state stand, and only the ledger row carries this fire's real result."""
    if job.get("last_run_at") != entry.get("at"):
        return False
    before, after = entry["before"], entry.get("after") or {}
    for field in _RESTORED:
        _put(job, field, before.get(field, _ABSENT))
    newer = {field: job.get(field, _ABSENT) for field in _DIAGNOSTICS
             if job.get(field, _ABSENT) != after.get(field, _ABSENT)}
    kept = {field: job.get(field, _ABSENT) for field in _NEWER_FIRE_FIELDS}
    record()
    for field, value in {**newer, **kept}.items():
        _put(job, field, value)
    return True
