"""Undo only this fire's dispatch reservation, only on proof no job work started."""

from __future__ import annotations

_REFUSAL_FIELDS = (
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
    "_refused_at",
)


class UpdateRefusedBeforeSubmit(RuntimeError):
    """Raised only at the pre-submit transport boundary (not from an agent's result text)."""


def restore_refused_slot(fire, *, observed_at: str | None = None):
    from cron import extra_fire
    from cron.jobs import (
        _fire_job_lock,
        _plan_fingerprint,
        _with_job,
        save_jobs,
    )

    prefix = "Update fence refused before cron.submit; no job work started. "

    def restore(jobs, index, job):
        job_claim = job.get("fire_claim")
        owner = (fire.get("fire_claim") or {}).get("by")
        if not owner or not isinstance(job_claim, dict) or job_claim.get("by") != owner:
            return prefix + "Claim changed; no rollback."
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
            # claim_dispatch spent a one-shot reservation under this fire claim before connect.
            # Undo only the precharge this worker actually made, only when restoring its plan.
            once = (fire.get("schedule") or {}).get("kind") == "once"
            old = fire.get("repeat") or {}
            repeat = job.get("repeat") or {}
            if (
                once
                and (old.get("times") or 0) > 0
                and not extra_fire.is_extra(fire)
                and repeat.get("times") == old.get("times")
                and repeat.get("completed") == old.get("completed", 0) + 1
                and job_claim.get("dispatch_precharged") == repeat.get("completed")
            ):
                repeat["completed"] -= 1
            job["next_run_at"] = slot
            if "_refused_manual_restore" in fire:
                job[extra_fire.RESTORE_KEY] = fire["_refused_manual_restore"]
            detail = prefix + "Original slot remains due."
            witness_at = observed_at or (fire.get("fire_claim") or {}).get("at")
            job["last_fire_error"] = {
                "at": witness_at,
                "detail": detail,
                "update_slot": slot,
                "update_at": witness_at,
            }
        else:
            detail = prefix + "No matching scheduled slot; current plan left alone."
        save_jobs(jobs)
        return detail

    with _fire_job_lock(fire["id"]) as acquired:
        if not acquired:
            return None  # retry rollback from the journal; do not book a failed run
        return _with_job(fire["id"], restore, prefix + "Job removed; no rollback.")


def verified_refusal_fire(
    record, job_id: str, execution_id: str, *, expected_owner: str | None = None
) -> dict | None:
    """Validate an admission journal or ledger fallback envelope; return its refusal snapshot."""
    if not isinstance(record, dict):
        return None
    params = record.get("params")
    fire = record.get("update_refused")
    if not isinstance(params, dict) or not isinstance(fire, dict):
        return None
    if (params.get("job_id"), params.get("request_id")) != (job_id, execution_id):
        return None
    if (fire.get("id"), fire.get("execution_id")) != (job_id, execution_id):
        return None
    owner = (fire.get("fire_claim") or {}).get("by")
    if not isinstance(owner, str) or not owner:
        return None
    if expected_owner is not None and owner != expected_owner:
        return None
    return fire


def load_verified_refusal(
    job_id: str, execution_id: str, *, expected_owner: str | None = None
) -> dict | None:
    """Read a verified pre-submit refusal for *(job_id, execution_id)* from the journal or ledger."""
    import json
    import sqlite3
    from cron import executions
    from cron.scheduler_authority import journal_path

    path = journal_path(job_id, execution_id)
    try:
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            fire = verified_refusal_fire(
                record, job_id, execution_id, expected_owner=expected_owner
            )
            if fire is not None:
                return fire
    except (OSError, ValueError):
        pass
    try:
        pending = executions.get_pending_refusal(execution_id, job_id=job_id)
    except (sqlite3.Error, OSError):
        pending = None
    if pending is not None:
        return verified_refusal_fire(
            pending, job_id, execution_id, expected_owner=expected_owner
        )
    return None


def reconcile_refusal_record(record: dict, job_id: str, request_id: str) -> None:
    """Replay one verified refusal journal record."""
    fire = verified_refusal_fire(record, job_id, request_id)
    if fire is None:
        raise ValueError("cron refusal identity conflict")
    settle_refusal(fire, recovered=True)


def reconcile_pending_refusals() -> None:
    """Replay refusal fallbacks persisted in executions.db when the admissions journal could not be written."""
    import logging
    from cron.executions import list_pending_refusals

    log = logging.getLogger(__name__)
    try:
        pending_refusals = list_pending_refusals()
    except Exception:  # health: allow BLE001 -- deferred to next tick like journal recovery
        log.warning("Cron pending refusal scan deferred", exc_info=True)
        return
    for eid, jid, envelope in pending_refusals:
        try:
            reconcile_refusal_record(envelope, jid, eid)
        except Exception:  # health: allow BLE001 -- deferred to next tick like journal recovery
            log.warning(
                "Cron pending refusal recovery deferred: %s/%s", jid, eid, exc_info=True
            )


def _persist_refusal_evidence(path, jid: str, eid: str, envelope: dict, log) -> None:
    import sqlite3
    from cron import executions
    from utils import atomic_json_write

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, envelope, mode=0o600, fsync_dir=True)
        return
    except OSError:
        log.warning(
            "Cron %s: refusal journal write failed; recording durable fallback in execution ledger",
            jid,
            exc_info=True,
        )
    try:
        executions.record_pending_refusal(eid, jid, envelope)
    except (sqlite3.Error, OSError):
        log.exception("Cron %s: refusal ledger fallback write failed", jid)


def settle_refusal(fire, *, recovered: bool = False):
    """Only replay the compare-and-restore, never the job. The two stores cannot commit together:
    keep the evidence until jobs.json and the ledger have both settled. No prompt/output in it."""
    import logging
    import sqlite3
    from cron import executions
    from cron.jobs import _hermes_now
    from cron.scheduler_authority import journal_path

    log = logging.getLogger(__name__)
    jid, eid = fire["id"], fire["execution_id"]
    path = journal_path(jid, eid)
    if recovered:
        observed_at = fire.get("_refused_at") or (fire.get("fire_claim") or {}).get("at")
    else:
        observed_at = fire.get("_refused_at") or _hermes_now().isoformat()
        snapshot = {k: fire[k] for k in _REFUSAL_FIELDS if k in fire}
        snapshot["_refused_at"] = observed_at
        envelope = {
            "params": {"job_id": jid, "request_id": eid},
            "update_refused": snapshot,
        }
        _persist_refusal_evidence(path, jid, eid, envelope, log)
        fire = snapshot
    try:
        detail = restore_refused_slot(fire, observed_at=observed_at)
    except (OSError, RuntimeError):
        log.exception("Cron %s: refused slot rollback pending", jid)
        detail = None
    reason = detail or "Update refused before cron.submit; slot restoration pending."
    args = dict(success=False, cancelled=True, error=reason, delivery_outcome="suppressed")
    if not executions.finish_execution(eid, **args):
        if not recovered:
            return reason
        row = executions.get_execution(eid)
        if row and row["status"] not in executions.FIRER_FINISHED_STATES:
            if not executions.recover_receipted_execution(eid, jid, **args):
                return reason  # a foreign live owner must settle the ledger itself
    if detail is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "Cron %s: could not remove settled refusal journal %s", jid, path, exc_info=True
            )
        try:
            executions.clear_pending_refusal(eid)
        except (sqlite3.Error, OSError):
            log.warning(
                "Cron %s: could not clear pending refusal fallback for %s",
                jid,
                eid,
                exc_info=True,
            )
    return reason
