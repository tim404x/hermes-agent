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
    # ``streak``: ``failure_streak`` right after this provisional failure. ``owes``: that streak is
    # still part of the job's (no run reset it since). ``counts``: the older pending fires whose
    # shares ``streak`` and ``before`` include. Together they let ``correct_history`` recompute the
    # streak in fire order when a newer run is booked before recovery replaces this outcome.
    counts = [eid for eid, entry in runs.items() if isinstance(entry, dict) and entry.get("owes")]
    runs[str(execution_id)] = {"before": before, "after": after, "at": job.get("last_run_at"),
                               "streak": int(job.get("failure_streak") or 0), "owes": True,
                               "counts": counts}
    job[KEY] = runs


def note_booked(job: dict[str, Any], execution_id: Optional[str]) -> None:
    """After any booking but a historical one: a run that reset the failure streak also erased every
    still-pending provisional fire's share of it, so their later correction must not subtract it."""
    if int(job.get("failure_streak") or 0) != 0:
        return
    for eid, entry in (job.get(KEY) or {}).items():
        if eid != execution_id and isinstance(entry, dict):
            entry["owes"] = False


def _fix_stamp(holder: dict[str, Any], entry: dict[str, Any], reset: bool, detail: str) -> None:
    """Correct *holder*'s ``last_failure`` while it is still this fire's provisional stamp."""
    stamp = holder.get("last_failure")
    if not (isinstance(stamp, dict) and entry.get("at") and stamp.get("at") == entry.get("at")):
        return
    if reset:
        _put(holder, "last_failure", (entry.get("before") or {}).get("last_failure", _ABSENT))
    else:
        holder["last_failure"] = dict(stamp, detail=detail)


def correct_history(job: dict[str, Any], entry: dict[str, Any], success: bool,
                    error: Optional[str], status: Optional[str]) -> None:
    """Recovery of an older fire after a newer run was booked: the newer run's outcome stands, but
    the streak and the sticky failure stamp are recomputed in fire order. The provisional mark
    counted this fire as one failure; a real success resets everything up to it (only failures
    booked after it remain), a real cancel counts nothing, a real failure stays counted. The stamp
    is this fire's only while no newer failure replaced it: a success restores the one before, a
    failure names its real error. Newer pending fires carry the same correction in their own
    ``streak`` and ``before`` snapshot, so their later recovery builds on it."""
    from cron.jobs_error_text import sanitize_persisted_error
    cancelled = status == "cancelled"
    reset = bool(success or cancelled)
    detail = sanitize_persisted_error(error) or (status or "run failed")
    share = (int(entry.get("streak") or 0) if success else 1) if reset and "streak" in entry else 0
    if share and entry.get("owes"):
        job["failure_streak"] = max(0, int(job.get("failure_streak") or 0) - share)
    from cron.jobs import _instant_after, _parse_aware
    mine, at = entry.get("_id"), _parse_aware(entry.get("at"))
    for other in (job.get(KEY) or {}).values():
        if not isinstance(other, dict) or at is None:
            continue
        other_at = _parse_aware(other.get("at"))
        if other_at is None or not _instant_after(other_at, at):
            if success:  # an older fire's share ended at this success
                other["owes"] = False
            continue
        if share and mine in (other.get("counts") or []):
            other["streak"] = max(0, int(other.get("streak") or 0) - share)
            before = other.get("before") or {}
            if isinstance(before.get("failure_streak"), int):
                before["failure_streak"] = max(0, before["failure_streak"] - share)
            if success:  # everything up to this fire was reset: older shares are gone too
                other["counts"] = [c for c in other.get("counts") or []
                                   if c != mine and c not in (entry.get("counts") or [])]
            else:
                other["counts"] = [c for c in other.get("counts") or [] if c != mine]
        if isinstance(other.get("before"), dict):
            _fix_stamp(other["before"], entry, reset, detail)
    _fix_stamp(job, entry, reset, detail)


def take(job: dict[str, Any], execution_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Pop the provisional entry this canonical mark replaces, or None when there is none."""
    runs = job.get(KEY)
    if execution_id is None or not isinstance(runs, dict):
        return None
    entry = runs.pop(str(execution_id), None)
    _prune(job)
    if not (isinstance(entry, dict) and isinstance(entry.get("before"), dict)):
        return None
    entry["_id"] = str(execution_id)  # popped, so never stored: ``correct_history`` finds its peers
    return entry


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
