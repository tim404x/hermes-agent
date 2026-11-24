"""Receipt recovery of a fire whose firer died, after a NEWER fire of the same job started.

The dead firer never marked its fire (no provisional entry), so recovery books it through
``mark_job_run``'s main path. Before this, that path treated the old fire as the latest run: it
cleared the fire claim, manual-run markers and ``pending_slot`` the newer fire owns, so the newer
fire's own completion was then refused as a stale owner ("fire claim owner changed; discarding stale
completion"), and it recomputed ``next_run_at`` over a manual run's kept slot.

Every fire claim carries ``seq``, the job's fire acquisition order (``fire_seq``), and each booking
records the highest booked one (``booked_fire_seq``). The recovered fire's claim identity is stamped
on its admission journal, so recovery can tell:

* a newer fire still pending (another owner holds the claim, or a run-now is queued): the old
  outcome is booked as the latest one (nothing newer is booked yet) and the fire is counted, but the
  newer fire's claim, markers and slot stay, and the schedule, re-run ladder and quota hold are left
  for the newer fire's booking.
* a newer fire already booked (``booked_fire_seq`` above this fire's ``seq``): the old outcome is
  historical. The fire is counted and its id recorded so a replay is a no-op; status, streak,
  schedule and claims stay with the newer run, and only its ledger row carries the real result (as
  ``jobs_provisional`` does when a provisional fire is recovered late).

An unknown own claim (a journal from before the stamp, a fire run without a claim) keeps the old
behaviour and clears the stored claim: a dead fire's own claim kept by mistake would block the job
until its TTL.
"""
from dataclasses import dataclass
from typing import Any, Optional

from cron.jobs_provisional import _NEWER_FIRE_FIELDS, _put

_ABSENT = "__absent__"
SEQ_KEY = "fire_seq"
BOOKED_KEY = "booked_fire_seq"


@dataclass(frozen=True)
class Order:
    pending: bool  # a newer fire holds the claim or a run-now is queued
    booked_after: bool  # a newer fire's outcome is already booked


def _seq(claim: Any) -> Optional[int]:
    seq = claim.get("seq") if isinstance(claim, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def next_seq(job: dict[str, Any]) -> int:
    """The acquisition order for a new fire claim (stamped under the jobs lock by the claim)."""
    seq = max(int(job.get(SEQ_KEY) or 0), int(job.get(BOOKED_KEY) or 0)) + 1
    job[SEQ_KEY] = seq
    return seq


def note_booked(job: dict[str, Any], claim: Any) -> None:
    """Record that the fire holding *claim* is booked, so an older fire recovered later is history."""
    seq = _seq(claim)
    if seq is not None and seq > int(job.get(BOOKED_KEY) or 0):
        job[BOOKED_KEY] = seq


def own_owner(claim: Any) -> Optional[str]:
    owner = claim.get("by") if isinstance(claim, dict) else None
    return owner if isinstance(owner, str) and owner else None


def own_extra(job: dict[str, Any], recovered_claim: Any) -> bool:
    """Whether the fire being booked is a manual run: a recovered fire's own claim says so, never
    the stored one (a newer fire's, or none after a newer booking cleared it)."""
    from cron import extra_fire
    if own_owner(recovered_claim) is not None:
        return extra_fire.is_extra({"fire_claim": recovered_claim})
    return extra_fire.is_extra(job)


def order(job: dict[str, Any], recovered_claim: Any, *, replacing: bool,
          provisional: bool) -> Optional[Order]:
    """Where a recovered fire stands against newer fires of *job*; None when it cannot tell (or
    this is no recovery). ``replacing``: its provisional mark already retired its own claim, so any
    stored claim is a newer fire's (``jobs_provisional.replace_outcome`` orders the outcome)."""
    from cron import extra_fire
    if provisional:
        return None
    stored = job.get("fire_claim")
    held = own_owner(stored)
    if replacing:
        return Order(pending=held is not None or extra_fire.queued(job), booked_after=False)
    owner = own_owner(recovered_claim)
    if owner is None:
        return None
    seq = _seq(recovered_claim)
    booked_after = seq is not None and int(job.get(BOOKED_KEY) or 0) > seq
    pending = (held is not None and held != owner) or extra_fire.queued(job)
    return Order(pending=pending, booked_after=booked_after)


def keep(job: dict[str, Any]) -> dict[str, Any]:
    """The newer fire's claim, markers and slot, before the old outcome is written."""
    return {field: job.get(field, _ABSENT) for field in _NEWER_FIRE_FIELDS}


def restore(job: dict[str, Any], kept: dict[str, Any]) -> None:
    for field, value in kept.items():
        _put(job, field, value)


def count(job: dict[str, Any], *, extra: bool, ladder_rung: bool, historical: bool) -> None:
    """Count the recovered fire's occurrence (``_advance_after_run``'s repeat rule) without moving
    the schedule, which belongs to the newer fire. A recurring job at its repeat limit retires now
    (a newer manual run then settles on the retired record). A historical one-shot fire is left to
    ``claim_dispatch``'s dispatch-limit guard: the record may have been re-armed since."""
    from cron.jobs import _complete_job_record
    if extra or ladder_rung:  # a manual run or a re-run of an occurrence already counted
        return
    kind = (job.get("schedule") or {}).get("kind")
    if kind == "once" and historical:
        return
    repeat = job.get("repeat")
    if repeat:
        times = repeat.get("times")
        finite = times is not None and times > 0
        completed = repeat.get("completed", 0)
        if not (kind == "once" and finite and completed > 0):  # claim_dispatch pre-counted it
            completed += 1
            repeat["completed"] = completed
        if finite and completed >= times:
            _complete_job_record(job)
            return
    if kind == "once":
        _complete_job_record(job)  # its scheduled fire ran
