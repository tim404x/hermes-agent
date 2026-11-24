"""Undo only this fire's dispatch reservation, only on proof no job work started."""


class UpdateRefusedBeforeSubmit(RuntimeError):
    """Raised only at the pre-submit transport boundary (not from an agent's result text)."""


def attach_tick_slot(claimed, scanned):
    """The claim normally sees a future slot: the built-in tick already advanced it. Bind
    the original raw instant to the worker only if it claimed the same scan and plan."""
    from cron.jobs import _plan_fingerprint

    manual = scanned.get("manual_run_at") == scanned.get("next_run_at")
    same_scan = claimed.get("run_claim") == scanned.get("run_claim") and claimed.get(
        "last_dispatch"
    ) == scanned.get("last_dispatch")
    same_dispatch = claimed.get("manual_run_at") == scanned.get("manual_run_at") and (
        same_scan or manual
    )
    claimed["_refused_slot"] = (
        scanned.get("next_run_at")
        if same_dispatch and _plan_fingerprint(claimed) == _plan_fingerprint(scanned)
        else None
    )
    if manual and "manual_run_restore_at" in scanned:
        claimed["_refused_manual_restore"] = scanned["manual_run_restore_at"]


def restore_refused_slot(fire):
    from cron import extra_fire
    from cron.jobs import (
        _fire_job_lock,
        _hermes_now,
        _plan_fingerprint,
        _with_job,
        save_jobs,
    )

    prefix = "Update fence refused before cron.submit; no job work started. "

    def restore(jobs, index, job):
        owner = (fire.get("fire_claim") or {}).get("by")
        if not owner or (job.get("fire_claim") or {}).get("by") != owner:
            return prefix + "Claim changed; no rollback."
        once = (fire.get("schedule") or {}).get("kind") == "once"
        # claim_dispatch spent a one-shot reservation before attempting the connection.
        # Undo that single increment, not a budget subsequently changed by another writer.
        old = fire.get("repeat") or {}
        repeat = job.get("repeat") or {}
        if (
            once
            and (old.get("times") or 0) > 0
            and not extra_fire.is_extra(fire)
            and repeat.get("times") == old.get("times")
            and repeat.get("completed") == old.get("completed", 0) + 1
        ):
            repeat["completed"] -= 1
        slot = fire.get("_refused_slot")
        # Heartbeats refresh a one-shot lease timestamp; the still-owned unique fire claim
        # and the run claim's attribution, not that changing timestamp, establish ownership.
        same_run_claim = (job.get("run_claim") or {}).get("by") == (
            fire.get("run_claim") or {}
        ).get("by")
        same_plan = (
            _plan_fingerprint(job) == _plan_fingerprint(fire)
            and job.get("next_run_at") == fire.get("next_run_at")
            and job.get("manual_run_at") == fire.get("manual_run_at")
            and same_run_claim
            and not job.get("pending_slot")
        )
        job.pop("fire_claim", None)
        if same_run_claim:
            job.pop("run_claim", None)
        if slot and same_plan:
            job["next_run_at"] = slot
            if "_refused_manual_restore" in fire:
                job[extra_fire.RESTORE_KEY] = fire["_refused_manual_restore"]
            detail = prefix + "Original slot remains due."
            job["last_fire_error"] = {
                "at": _hermes_now().isoformat(),
                "detail": detail,
                "update_slot": slot,
            }
        else:
            detail = prefix + "No matching scheduled slot; current plan left alone."
        save_jobs(jobs)
        return detail

    with _fire_job_lock(fire["id"]) as acquired:
        if not acquired:
            return None  # retry rollback from the journal; do not book a failed run
        return _with_job(fire["id"], restore, prefix + "Job removed; no rollback.")


def settle_refusal(fire, *, recovered=False):
    """Only replay the compare-and-restore, never the job. The two stores cannot commit together:
    keep the evidence until jobs.json and the ledger have both settled. No prompt/output in it."""
    import logging
    from cron import executions
    from cron.scheduler_authority import journal_path
    from utils import atomic_json_write

    jid, eid = fire["id"], fire["execution_id"]
    path = journal_path(jid, eid)
    if not recovered:
        fields = (
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
            "_refused_manual_restore",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            path,
            {
                "params": {"job_id": jid, "request_id": eid},
                "update_refused": {k: fire[k] for k in fields if k in fire},
            },
            mode=0o600,
            fsync_dir=True,
        )
    try:
        detail = restore_refused_slot(fire)
    except OSError:
        logging.getLogger(__name__).exception(
            "Cron %s: refused slot rollback pending", jid
        )
        detail = None
    reason = detail or "Update refused before cron.submit; slot restoration pending."
    if recovered:
        row = executions.get_execution(eid)
        if row and row["status"] not in executions.FIRER_FINISHED_STATES:
            if not executions.recover_receipted_execution(
                eid,
                jid,
                success=False,
                error=reason,
                cancelled=True,
                delivery_outcome="suppressed",
            ):
                return reason  # the ledger still has an owner; keep the journal
    elif not executions.finish_execution(
        eid, success=False, cancelled=True, error=reason, delivery_outcome="suppressed"
    ):
        return reason
    if detail is not None:
        path.unlink(missing_ok=True)
    return reason
