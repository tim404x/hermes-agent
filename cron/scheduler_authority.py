"""Transport-only cron execution; output/delivery remain with the firing scheduler."""
import asyncio
import hashlib
import json
import logging
import random
import time
import uuid

from hermes_cli.gateway_client import GatewayRPCError
from storage_errors import is_transient_store_failure

logger = logging.getLogger(__name__)


class CronExecutionUnknown(RuntimeError):
    """The owner may have accepted this fire; never book it as failed or re-fire."""


# How long a firer whose gateway connection dropped mid-fire keeps redialing (backoff from
# RECONNECT_FIRST_DELAY_S, capped at RECONNECT_MAX_DELAY_S) before it gives the fire up as
# unknown. Covers a stalled owner loop and a gateway restart; a gateway down longer fails closed.
RECONNECT_BUDGET_S = 600.0
RECONNECT_FIRST_DELAY_S, RECONNECT_MAX_DELAY_S = 0.5, 30.0
STORAGE_RETRY_DELAYS_S = (1.0, 3.0, 8.0, 15.0)
STATUS_STORAGE_BUDGET_S = 120.0


def _transient_storage_refusal(exc: BaseException) -> bool:
    """Older gateways sent only the reason; newer ones can explicitly rule out a retry. Only the
    store-failure code (5001, or no code from an older owner) is retryable: a 4001 policy refusal
    that names ``storage_unavailable`` is permanent, as it is owner-locally (AuthorityRefusal)."""
    if isinstance(exc, GatewayRPCError):
        return exc.code in (5001, None) and exc.data.get('transient') is not False and (
            str(exc) == 'storage_unavailable' or exc.data.get('reason') == 'storage_unavailable')
    return is_transient_store_failure(exc)


class _StorageRetryStopped(Exception):
    """Cancellation while waiting to resubmit a fire ``recover`` proved missing: nothing is admitted,
    so the stop is booked like a refusal. A cancel during a RECOVER backoff is not this: the
    admission is unproven there, so it fails closed (CronExecutionUnknown, receipt recovery)."""


async def _wait_storage_retry(delay, cancel_event):
    if cancel_event is None:
        await asyncio.sleep(delay)
        return
    while not cancel_event.is_set():
        if delay <= 0:
            return
        step = min(delay, 0.5)
        await asyncio.sleep(step)
        delay -= step
    raise _StorageRetryStopped


class _ConnectionLost(Exception):
    """An observer RPC lost its socket (disconnect, reply timeout): the owner never answered, so
    nothing is known about the fire. Not a ``GatewayClientError`` subclass on purpose:
    ``connect_gateway`` then lets it through unchanged, so it is never mistaken for a failure of
    the firer's own work (a journal write) that the dial wraps the same way."""


def journal_path(job_id, request_id):
    from hermes_constants import get_hermes_home
    key = hashlib.sha256(json.dumps([job_id, request_id]).encode()).hexdigest()
    return get_hermes_home() / 'cron' / 'admissions' / (key + '.json')


def _pause_identity(paused):
    """The exact pause this recovery uncertainty caused, so only that pause is ever lifted (``True``:
    nothing was paused, the job was already held). ``plan`` lets the lift notice a schedule edited
    while the job was held."""
    if not paused:
        return True
    from cron.jobs import _plan_fingerprint
    return {'paused_at': paused.get('paused_at'), 'reason': paused.get('paused_reason'),
            'plan': _plan_fingerprint(paused)}


def _claim_identity(claim):
    """The parts of a fire claim that identify its fire (owner, acquisition order, extra-fire
    slot), not its lease."""
    if not isinstance(claim, dict) or not claim.get('by'):
        return None
    from cron.extra_fire import _CLAIM_KEY
    return {key: claim[key] for key in ('by', 'seq', _CLAIM_KEY) if claim.get(key)}


def _recovered_claim(record, job):
    """The claim the recovered fire held: its journal's stamp, else (a journal written before the
    stamp) the job snapshot the owner froze at admission or the refusal recorded. None when unknown:
    the current job's claim (a ``missing`` admission) may be a newer fire's."""
    if 'fire_claim' in record:
        return record['fire_claim']
    return _claim_identity(job.get('fire_claim')) if job is not None else None


def replayable_completions(job_id, completions):
    """The recorded completion ids whose admission journal still exists: only journal recovery
    can replay a completion into ``mark_job_run``, so an id without one is a dead tombstone."""
    return [eid for eid in completions or () if journal_path(job_id, eid).exists()]


def _booking_stamp(delivery, recovered_claim, aged_at):
    """What booking a recovered fire needs once its output is saved and its notice delivered: the
    job as composed (run flags included) and the delivery verdicts. Stamped on the journal before
    the booking, so a booking the store refuses (a newer fire holds the fire fence past its
    timeout) is retried from it on the next tick without saving or delivering the output again,
    and without asking the owner, which may have forgotten the admission by then. ``aged_at``: when
    the quota hold was last aged, so a retry holds only what is left of the window."""
    from dataclasses import fields

    from cron.jobs_error_text import sanitize_persisted_error
    from cron.quota_hold import HOLD_FROM_KEY
    job = json.loads(json.dumps(dict(delivery.job), default=str))
    if job.get('_quota_hold_seconds'):
        job[HOLD_FROM_KEY] = aged_at
    if not delivery.should_deliver:
        # The first booking clears a stale receipt; a retry must not clear a newer fire's.
        job['last_delivery_queued'] = None
    verdicts = {f.name: getattr(delivery, f.name) for f in fields(delivery) if f.name != 'job'}
    for key in ('error', 'delivery_error'):  # stored the way jobs.json stores them
        if verdicts.get(key):
            verdicts[key] = sanitize_persisted_error(str(verdicts[key]))
    return {'job': job, 'delivery': json.loads(json.dumps(verdicts, default=str)),
            'recovered_claim': recovered_claim}


def _from_booking_stamp(stamp):
    from cron.quota_hold import age_hold_flag
    from cron.scheduler import _RunDelivery
    job = dict(stamp['job'])
    age_hold_flag(job)
    verdicts = dict(stamp.get('delivery') or {})
    return _RunDelivery(job, verdicts.pop('success'), verdicts.pop('error', None), **verdicts)


def pause_for_unknown(job_id, execution_id, reason):
    """The firer pauses the job for this fire's unverified admission and records that exact pause
    on the fire's journal, so the receipt that later proves the run terminal can lift it."""
    from cron.jobs import hold_for_unknown
    from utils import atomic_json_write
    paused = hold_for_unknown(job_id, reason)  # an operator's pause is never adopted
    journal = journal_path(job_id, execution_id)
    if paused and journal.exists():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        record['pause_recorded'] = _pause_identity(paused)
        atomic_json_write(journal, record, mode=0o600, fsync_dir=True)


def _lift_pause(job_id, pause):
    """A verified terminal receipt resolved the uncertainty that paused the schedule: resume it iff
    the job is still held by exactly that pause (an operator's own pause/resume is never touched).
    ``mark_job_run(lift_pause=...)`` already lifts it inside the outcome's own write, before the
    retry ladder and quota hold plan (both refuse a paused job); this is the fallback for a booking
    that never reached the store. Either way ``next_run_at`` stays as booked (``resume_job`` would
    recompute it from now and drop a ladder rung, a quota hold or a manual run's kept slot)."""
    from cron.jobs import lift_recovery_pause
    lift_recovery_pause(job_id, pause)


# A parked notice is retried this many times when its delivery fails, then dropped with a warning.
_PARKED_NOTICE_ATTEMPTS = 3


def _notice_path(journal):
    """Sidecar of an admission journal holding its parked unknown-outcome notice. Its own file, so
    recovery's journal rewrites (``pause_recorded``) can never drop it, and not ``*.json``, so the
    journal scan never reads it as an admission."""
    return journal.with_suffix('.notice')


def _builtin_ticker():
    """Receipt recovery (``reconcile_pending``) runs only in the built-in ticker's housekeeping."""
    try:
        from hermes_cli.config import cfg_get, load_config
        name = (cfg_get(load_config(), 'cron', 'provider', default='') or '').strip()
    except Exception:
        return True
    return name in ('', 'builtin', 'in-process', 'inprocess')


def park_unknown_notice(job, execution_id, notice):
    """Hold the failure notice of a fire whose firer gave up as unknown while its admission journal
    still exists: the owner may have finished the run (a restart cut only the firer's connection), and
    receipt recovery (``reconcile_pending``) books and delivers the real outcome. The notice goes out
    only if recovery settles the fire ``unknown``. False (send it now) when there is no journal or no
    built-in ticker to run that recovery."""
    from utils import atomic_json_write
    journal = journal_path(job['id'], execution_id)
    if not journal.exists() or not _builtin_ticker():
        return False
    snapshot = json.loads(json.dumps(dict(job), default=str))
    atomic_json_write(_notice_path(journal), {'notice': str(notice or ''), 'job': snapshot, 'attempts': 0},
                      mode=0o600, fsync_dir=True)
    return True


def _send_parked_notice(journal):
    """Deliver a parked unknown-outcome notice once recovery proved the outcome is really unknown.
    Sent first and cleared after, so a crash can repeat it but never lose it; a failing delivery is
    retried on later ticks, ``_PARKED_NOTICE_ATTEMPTS`` times at most."""
    import logging
    from cron import scheduler
    from cron.jobs import get_job
    from utils import atomic_json_write
    path = _notice_path(journal)
    try:
        parked = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return
    job = dict(parked.get('job') or {})
    current = get_job(job.get('id')) if job.get('id') else None
    job = dict(current or job, execution_id=job.get('execution_id'))  # a removed one-shot keeps its snapshot
    tokens = scheduler._install_fire_secret_scope()
    try:
        delivery_error, _ = scheduler._deliver_crash_failure(job, parked.get('notice') or '', adapters=None, loop=None)
    except Exception as exc:  # health: allow BLE001 -- retried on the next tick
        delivery_error = str(exc)
    finally:
        scheduler._reset_fire_secret_scope(tokens)
    attempts = int(parked.get('attempts') or 0) + 1
    if not delivery_error or attempts >= _PARKED_NOTICE_ATTEMPTS:
        if delivery_error:
            logging.getLogger(__name__).warning(
                'Cron %s unknown-outcome notice not delivered after %d attempts: %s', job.get('id'), attempts, delivery_error)
        path.unlink(missing_ok=True)
    else:
        parked['attempts'] = attempts
        atomic_json_write(path, parked, mode=0o600, fsync_dir=True)


def run_canonical_job(job, *, extra_prompt=None, cancel_event=None, execution_id=None):
    from gateway.session_cron import owner_for_home, operation
    from hermes_cli.gateway_client import connect_gateway
    from hermes_constants import get_hermes_home
    from storage_user_copy import error_type_name
    from utils import atomic_json_write

    request_id = execution_id or job.get('execution_id')
    if not request_id:
        request_id = job['execution_id'] = uuid.uuid4().hex
    params = {'job_id': job['id'], 'request_id': request_id,
              'extra_prompt': extra_prompt}
    root = get_hermes_home() / 'cron' / 'admissions'
    journal = journal_path(params['job_id'], params['request_id'])
    # The fire claim this fire holds (its recovery must tell it from a newer fire's) and the slot it
    # fired for (a retry rung is not a fresh occurrence): the owner's frozen job carries neither.
    record = {'params': params, 'receipt': None, 'fire_claim': _claim_identity(job.get('fire_claim')),
              'scheduled_instant': job.get('_scheduled_instant')}
    if journal.exists():
        record = json.loads(journal.read_text(encoding='utf-8-sig'))
        if record['params'] != params:
            raise CronExecutionUnknown('cron admission identity conflict')
    attempted = journal.exists()
    owner = owner_for_home(get_hermes_home())

    def write_journal():
        root.mkdir(parents=True, exist_ok=True)
        atomic_json_write(journal, record, mode=0o600, fsync_dir=True)

    async def save():
        # ``observe`` runs on the owner loop when this process owns the profile: the fsynced journal
        # write (file and directory) waits in a worker, and still completes before the next step.
        await asyncio.to_thread(write_journal)

    delays = iter(STORAGE_RETRY_DELAYS_S)
    resubmits = 0

    def retry_delay(exc):
        delay = next(delays, None) if _transient_storage_refusal(exc) else None
        return delay * (1 + random.uniform(0, 0.25)) if delay is not None else None

    async def refuse(exc, unreachable=False):
        """The owner answered the submit with an error and holds no admission: the booked failure."""
        error = f'{error_type_name(exc)}: {exc}'
        if unreachable:
            job['_model_unreachable'] = True
            error += f' (transient storage contention persisted through {resubmits} resubmits)'
        result = [False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error]
        record['refusal'] = result
        record['job'] = dict(job)
        await save()
        return tuple(result)

    # A submit the owner answered with an error, and the last owner progress stamp mirrored into
    # our ledger row: both outlive a dropped connection (a redial must neither forget the refusal
    # nor re-stamp a wedged run's unchanged progress as fresh).
    answered = progress = None

    async def recover(call):
        while True:
            try:
                return await call('recover', params)
            except Exception as exc:
                delay = retry_delay(exc)
                if delay is None:
                    raise
                logger.info('Cron %s: recover read refused by transient storage contention (%s); retrying in %.1fs',
                            job['id'], error_type_name(exc), delay)
                try:
                    await _wait_storage_retry(delay, cancel_event)
                except _StorageRetryStopped:
                    # The submit may have committed before its error came back: never book this.
                    raise CronExecutionUnknown(
                        f'cron admission unverified: stopped while recover was refused ({error_type_name(exc)})'
                    ) from exc

    async def submit(call):
        nonlocal attempted, answered, resubmits
        attempted = True
        await save()
        while True:
            answered = None  # a dropped resubmit is ambiguous regardless of the previous answer
            try:
                return await call('submit', params)
            except Exception as exc:
                if owner is None and not isinstance(exc, GatewayRPCError):
                    raise
                answered = exc
                state = await recover(call)
                if state['status'] in {'queued', 'started', 'terminal'}:
                    return {'session_id': state['session_id'], 'admission_id': state['admission_id']}
                if state['status'] != 'missing':
                    raise
                delay = retry_delay(exc)
                if delay is None:
                    return await refuse(exc, unreachable=_transient_storage_refusal(exc))
                resubmits += 1
                logger.info('Cron %s: submit refused by transient storage contention (%s); resubmitting '
                            'execution %s in %.1fs (attempt %d/%d)', job['id'], error_type_name(exc),
                            request_id, delay, resubmits, len(STORAGE_RETRY_DELAYS_S))
                await _wait_storage_retry(delay, cancel_event)

    async def readmit(call):
        """A reconnect's receipt for a fire whose submit answer went down with the socket: read
        back by ``recover``, never submitted again. ``missing`` cannot prove a lost submit is not
        still in flight on the owner, so it fails closed and leaves the journal to receipt recovery;
        only a submit the owner already answered with an error (``answered``) books that refusal."""
        state = await call('recover', params)
        if state['status'] == 'missing' and answered is not None:
            return await refuse(answered)
        if state['status'] not in {'queued', 'started', 'terminal'}:
            raise CronExecutionUnknown(f'cron admission {state["status"]} after the gateway connection dropped')
        return {'session_id': state['session_id'], 'admission_id': state['admission_id']}

    status_refused_at = None

    async def poll(call, receipt):
        nonlocal status_refused_at
        delay = .1
        while True:
            if cancel_event is not None and cancel_event.is_set():
                try:
                    await call('cancel', receipt)
                except Exception as exc:
                    if not _transient_storage_refusal(exc):
                        raise
            try:
                state = await call('status', receipt)
            except Exception as exc:
                if not _transient_storage_refusal(exc):
                    raise
                now = time.monotonic()
                if status_refused_at is None:
                    status_refused_at = now
                    logger.info('Cron %s: status read refused by transient storage contention (%s); retrying',
                                job['id'], error_type_name(exc))
                elif now - status_refused_at >= STATUS_STORAGE_BUDGET_S:
                    raise
            else:
                status_refused_at = None
                yield state
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)

    async def observe(call, *, resumed=False):
        """Admit the fire (``resumed``: on a fresh connection after a drop, from the saved receipt
        or ``recover``) and poll its receipt to the outcome."""
        nonlocal progress
        receipt = record['receipt'] if resumed else None
        if receipt is None:
            try:
                receipt = await (readmit if resumed else submit)(call)
            except _StorageRetryStopped:
                from cron.cancellation import cancel_reason
                return await refuse(RuntimeError(f"Cron job '{job.get('name') or job['id']}' was stopped: "
                                                 f"{cancel_reason(cancel_event)}"))
            if isinstance(receipt, tuple):
                return receipt  # the owner refused the submit: the booked failure
            record['receipt'] = receipt
            await save()
        async for state in poll(call, receipt):
            if state.get('progress_at') not in (None, progress):
                # The owner's agent is still working: refresh OUR ledger row (the owner's stamp is
                # fenced to the row's owner), once per owner stamp, so the stale-claim sweep measures
                # the run's silence. A wedged owner stops stamping and the sweep still fires.
                from cron.executions import touch_execution_progress
                progress = state['progress_at']
                await asyncio.to_thread(touch_execution_progress, request_id)
            if state['status'] == 'terminal':
                job.update(state.get('job_flags') or {})
                from cron.quota_hold import age_hold_flag
                age_hold_flag(job)
                return tuple(state['result'])
            if state['status'] == 'unknown':
                raise CronExecutionUnknown('unknown_execution: cron admission was not replayed')

    async def remote():
        """Observe over the gateway socket. The one socket lives for the whole agent turn (30+ min);
        once the fire is journaled, a lost socket says nothing about the run (a stalled owner loop
        failed a ``cron.status`` reply, 24 Nov 2026): redial with backoff and resume from the saved
        receipt, never submitting again. Only a gateway unreachable for ``RECONNECT_BUDGET_S``
        gives the fire up as unknown."""
        from hermes_cli.gateway_client import GatewayClientError
        resumed, lost_at, backoff = False, None, RECONNECT_FIRST_DELAY_S
        while True:
            dialed = False
            try:
                async with connect_gateway() as client:
                    dialed = True

                    async def call(op, data):
                        nonlocal lost_at, backoff
                        try:
                            answer = await client.rpc('cron.' + op, **data)
                        except GatewayRPCError:
                            raise  # the owner answered: definite, never a reason to redial
                        except (GatewayClientError, OSError, TimeoutError) as exc:
                            raise _ConnectionLost(str(exc) or error_type_name(exc)) from exc
                        if op != 'cancel':  # only an observation proves the owner is back
                            lost_at, backoff = None, RECONNECT_FIRST_DELAY_S
                        return answer
                    return await observe(call, resumed=resumed)
            except Exception as exc:
                # A dial refused before anything was journaled is no attempt at all. A failed redial
                # (refused ticket, upgrade cut by a restarting owner) is transport like a lost socket;
                # out of an open connection anything else is final: an owner answer, the firer's own
                # journal write, or ``CronExecutionUnknown``.
                if not attempted or (dialed and not isinstance(exc, _ConnectionLost)):
                    raise
                now = time.monotonic()
                lost_at = now if lost_at is None else lost_at
                left = RECONNECT_BUDGET_S - (now - lost_at)
                if left <= 0:
                    raise
                logging.getLogger(__name__).warning(
                    'Cron %s: gateway connection lost while observing execution %s (%s); redialing in %.1fs '
                    '(%.0fs of reconnect budget left)', job['id'], request_id, exc, min(backoff, left), left)
                await asyncio.sleep(min(backoff, left))
                resumed, backoff = True, min(backoff * 2, RECONNECT_MAX_DELAY_S)

    try:
        if owner is not None:
            authority, loop = owner
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is loop:
                raise RuntimeError('cron synchronous execution must run off the owner event loop')
            return asyncio.run_coroutine_threadsafe(
                observe(lambda op, data: operation(authority, op, data)), loop).result()
        return asyncio.run(remote())
    except Exception as exc:
        if attempted:
            raise CronExecutionUnknown(f'Cron execution unverified; reconcile {journal}: {exc}') from exc
        from hermes_cli.gateway_client import GatewayUnavailableError
        from cron.update_refusal import UpdateRefusedBeforeSubmit
        if (isinstance(exc, GatewayUnavailableError) and exc.state == 'draining'
                and exc.reason_code == 'update_paused' and job.get('fire_claim')
                and not job.get('_monitor_source_started')):
            # No prior journal, no submit, and no user monitor source ran in this worker.
            # A terminal result with exactly this text (or a disconnect after submit) is NOT proof.
            raise UpdateRefusedBeforeSubmit(str(exc)) from exc
        error = f'{error_type_name(exc)}: {exc}'
        return False, f'# Cron Job: {job["id"]} (FAILED)\n\n{error}\n', '', error

def reconcile_pending(*, allow_connect=True):
    """Observe prepared fires; never submit missing or interrupted work.

    With allow_connect=False, a headless tick leaves durable receipts for the next
    live owner instead of ensuring or spawning a gateway.
    """
    from gateway.session_cron import owner_for_home, operation
    from hermes_constants import get_hermes_home
    from hermes_cli.gateway_client import connect_gateway
    from cron.executions import FIRER_FINISHED_STATES, execution_owner_live, get_execution
    from cron import jobs_newer_fire
    from cron.jobs import get_job, hold_for_unknown
    from hermes_time import now as _hermes_now
    from cron.scheduler import (_RunDelivery, _FireOwnership, _apply_agent_failure_marker,
                                _save_compose_deliver)
    from cron.scheduler_bookkeeping import fail_empty_response, finish_completed_run
    from utils import atomic_json_write
    import logging

    root = get_hermes_home() / 'cron' / 'admissions'
    for parked in root.glob('*.notice'):
        if not parked.with_suffix('.json').exists():
            parked.unlink(missing_ok=True)  # its fire was settled: the real outcome went out instead
    for journal in sorted(root.glob('*.json')):
        try:
            record = json.loads(journal.read_text(encoding='utf-8-sig'))
            params = record['params']
            if journal != journal_path(params['job_id'], params['request_id']):
                raise ValueError('cron journal identity conflict')
            if execution_owner_live(params['request_id'], params['job_id']):
                # Its firer (this process included) still owns the row and its tail: re-saving the
                # output or re-marking the job on every tick would duplicate its side effects.
                continue
            if 'update_refused' in record:
                from cron.update_refusal import settle_refusal
                fire = record['update_refused']
                if (fire['id'], fire['execution_id']) != (params['job_id'], params['request_id']):
                    raise ValueError('cron refusal identity conflict')
                settle_refusal(fire, recovered=True)
                continue
            row = get_execution(params['request_id'])
            if row is not None and row['job_id'] == params['job_id'] and row['status'] in FIRER_FINISHED_STATES:
                # The firer booked this attempt's terminal outcome itself (ownership lost, shutdown
                # interrupt, a crashed tail): the receipt is not a result to save or deliver again.
                journal.unlink(missing_ok=True)
                continue
            if isinstance(record.get('booking'), dict):
                # Output saved and notice delivered on an earlier tick; only the booking is left.
                stamp = record['booking']
                if finish_completed_run(_from_booking_stamp(stamp), None, params['request_id'],
                                        recovered=True, lift_pause=record.get('pause_recorded'),
                                        recovered_claim=stamp.get('recovered_claim')):
                    _lift_pause(params['job_id'], record.get('pause_recorded'))
                continue
            frozen = None  # the job as admitted, never the current record
            if 'refusal' in record:
                state = {'status': 'terminal', 'result': record['refusal'], 'job': record['job']}
                frozen = record['job']
            else:
                owner = owner_for_home(get_hermes_home())
                if owner is None and not allow_connect:
                    continue
                async def observe():
                    if owner is not None:
                        return await operation(owner[0], 'recover', params)
                    async with connect_gateway() as client:
                        return await client.rpc('cron.recover', **params)
                if owner is not None:
                    state = asyncio.run_coroutine_threadsafe(observe(), owner[1]).result(timeout=20)
                else:
                    state = asyncio.run(observe())
                frozen = state.get('job')
            if state['status'] == 'missing':
                # The owner holds no admission for this fire: a definite non-admission, booked as
                # the same failed run the in-submit refusal books. Only ``unknown`` pauses.
                current = get_job(params['job_id'])
                if current is None:
                    journal.unlink(missing_ok=True)
                    continue
                error = 'Cron admission missing: the owner never admitted this fire'
                state = {'status': 'terminal', 'job': current,
                         'result': [False, f'# Cron Job: {params["job_id"]} (FAILED)\n\n{error}\n', '', error]}
            if state['status'] != 'terminal':
                if state['status'] == 'unknown' and _notice_path(journal).exists():
                    # Recovery confirmed the outcome is unknown: the parked notice is true now. Sent
                    # before the pause below, which a finished one-shot refuses.
                    _send_parked_notice(journal)
                if state['status'] == 'unknown' and not record.get('pause_recorded'):
                    reason = 'Canonical cron unknown; no automatic re-execution'
                    record['pause_recorded'] = _pause_identity(hold_for_unknown(params['job_id'], reason))
                    atomic_json_write(journal, record, mode=0o600, fsync_dir=True)
                continue
            success, output, answer, error = state['result']
            job = dict(state['job'], **(state.get('job_flags') or {}))
            from cron.quota_hold import age_hold_flag
            age_hold_flag(job)  # a receipt read late holds only what is left of the window
            aged_at = _hermes_now().isoformat()
            job['execution_id'] = params['request_id']
            if record.get('scheduled_instant'):
                job['_scheduled_instant'] = record['scheduled_instant']
            # The same result verdicts as the ordinary tail: an agent-declared [CRON_FAILURE] and a
            # blank answer are failures, whichever process books them.
            success, error, declared = _apply_agent_failure_marker(job, success, error, answer)
            recovered_claim = _recovered_claim(record, frozen)
            # The booking will leave the re-run ladder and quota hold to a newer fire: the notice
            # must neither announce a hold nor wait for a re-run that is never planned. Only the
            # notice is composed without them; the fenced booking still gets the real flags and
            # decides against the store as it is then.
            masked = {}
            if jobs_newer_fire.planners_skipped(get_job(params['job_id']), params['request_id'],
                                                recovered_claim):
                masked = {key: job.pop(key) for key in ('_quota_hold_seconds', '_model_unreachable')
                          if key in job}
            delivery = _RunDelivery(job, success, error, agent_declared=declared)
            # The canonical receipt supplies the result; a dead scheduler's fire claim does not
            # authorize replay and must not prevent finishing its durable bookkeeping.
            fence = _FireOwnership(dict(job, fire_claim=None))
            try:
                _save_compose_deliver(delivery, fence, answer, output, adapters=None, loop=None,
                                     verbose=False, execution_token=None)
            finally:
                job.update(masked)
            fail_empty_response(delivery, answer)
            record['booking'] = _booking_stamp(delivery, recovered_claim, aged_at)
            atomic_json_write(journal, record, mode=0o600, fsync_dir=True)
            if finish_completed_run(delivery, None, params['request_id'], recovered=True,
                                    lift_pause=record.get('pause_recorded'),
                                    recovered_claim=recovered_claim):
                _lift_pause(params['job_id'], record.get('pause_recorded'))
        except Exception:
            logging.getLogger(__name__).warning('Cron receipt recovery deferred: %s', journal, exc_info=True)
