"""Job-level visibility for a recovered, acknowledged worker death."""


def record_unknown_worker_outcome(
    job: dict, *, error=None, adapters=None, loop=None
) -> bool:
    """Project only this fire's uncertain outcome, without retrying its side effects.

    A terminal ledger row alone does not retire the job claim or notify its owner.
    Fence the notice and bookkeeping together so recovery cannot overwrite a newer
    fire or duplicate a failure the worker already recorded.
    """
    from cron import scheduler
    from cron.unreachable_retry import is_retry_run

    execution = scheduler.get_execution(str(job["execution_id"]))
    if not execution or execution.get("status") != "unknown":
        return False
    claim = job.get("fire_claim")
    owner = str(claim.get("by") or "") if isinstance(claim, dict) else ""
    if not owner:
        return False
    from cron.update_refusal import load_verified_refusal, settle_refusal

    refusal = load_verified_refusal(
        str(job["id"]), str(job["execution_id"]), expected_owner=owner
    )
    if refusal is not None:
        settle_refusal(refusal, recovered=True)
        return True
    with scheduler.fire_claim_fence(job["id"], expected_owner=owner) as owns_claim:
        if not owns_claim:
            return True
        error = error or execution["error"]
        scheduler.logger.error("Job '%s': %s", job["id"], error)
        scope_tokens = scheduler._install_fire_secret_scope()
        delivery_error = None
        try:
            from cron.scheduler_authority import park_unknown_notice
            # A worker that lost the gateway mid-observation leaves its admission journal: the run
            # may have completed (a restart cut only the connection). Receipt recovery delivers the
            # real outcome; the notice waits on the journal and goes out only if that outcome is
            # unknown. Without a journal nothing will settle it later, so tell the owner now.
            if not park_unknown_notice(job, job["execution_id"], error):
                delivery_error, _ = scheduler._deliver_crash_failure(
                    job, error, adapters=adapters, loop=loop
                )
        finally:
            scheduler._reset_fire_secret_scope(scope_tokens)
            scheduler.mark_job_run(
                job["id"],
                False,
                error,
                delivery_error=delivery_error,
                expected_fire_owner=owner,
                # Receipt recovery may still book this fire's real outcome: that mark replaces this
                # one instead of counting the fire twice (cron/jobs_provisional.py).
                execution_id=str(job["execution_id"]),
                provisional=True,
                # A ladder re-run's occurrence already counted toward repeat.
                **({"ladder_rung": True} if is_retry_run(job) else {}),
            )
    return True


def external_worker_exited_reason(returncode: int | None) -> str:
    """Cause for an attempt whose restart-safe worker this process watched exit.

    The generic dead-owner sweep can only say the owner vanished, so it names a
    scheduler restart — but this waiter never lost its scheduler: it handed the
    attempt to the external worker and then saw that worker exit with this status.
    The run is still ``unknown`` rather than ``failed`` (whether side effects ran is
    still unknown); only the cause becomes truthful (#128509).
    """
    return (
        f"Restart-safe cron worker exited with status {returncode} after adopting this "
        "execution, without writing a durable terminal state; whether side effects ran "
        "is unknown."
    )
