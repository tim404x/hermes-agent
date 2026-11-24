"""Receipt recovery whose booking the store refuses keeps the journal and retries the booking.

GPT 6.2 review of t_cd677be5 (24 Nov 2026, finding 8): a newer fire B held the job's fire fence past
the fence timeout while recovery booked old fire A. ``mark_job_run`` returned False, but
``finish_completed_run`` only handled a refused mark for a fire owner, so it settled A's ledger row
and deleted A's journal: A was never counted and nothing retried it. Now the journal and the ledger
row stay, the journal carries a booking stamp written after the delivery, and the next tick books A
from that stamp without saving or delivering its output again. A removed job is finalized as before.

Finding 9: a recovered fire booked under a newer fire leaves the re-run ladder and quota hold to that
fire, so its notice must not announce a hold (nor wait for a re-run) that is never planned.
"""
import contextvars
import json
import threading

import pytest

from cron import executions, jobs, scheduler, scheduler_authority
from tests.cron.test_parked_unknown_notice import _notices, _peer
from tests.cron.test_recovery_keeps_newer_fire import _claim_b, _dead_firer_fire


@pytest.fixture
def store(tmp_path, monkeypatch):
    _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        yield


@pytest.fixture
def side_effects(monkeypatch):
    """Count output saves and queued notices (the recovered fire's two side effects)."""
    seen = {'saved': 0, 'queued': []}
    real_save = scheduler.save_job_output

    def save(job_id, output):
        seen['saved'] += 1
        return real_save(job_id, output)

    def enqueue(execution_id, job, content, *, for_failure=False):
        seen['queued'].append((execution_id, content))
        return {'execution_id': execution_id, 'status': 'pending'}

    from cron import delivery_queue
    monkeypatch.setattr(scheduler, 'save_job_output', save)
    monkeypatch.setattr(delivery_queue, 'enqueue', enqueue)
    return seen


class _Owner:
    """The owner answers the recovered fire's receipt once, then forgets the admission."""

    def __init__(self, monkeypatch, job_id, result, **flags):
        self.calls = 0

        def answer(method):
            self.calls += 1
            if self.calls > 1:
                return {'status': 'missing'}
            return {'status': 'terminal', 'job': jobs.get_job(job_id), 'result': result,
                    'job_flags': flags}
        _peer(monkeypatch, answer)


@pytest.fixture
def busy_fence(monkeypatch):
    """Fire B holds the job's fire fence (another thread) past a short fence timeout."""
    monkeypatch.setattr(jobs, '_JOBS_LOCK_TIMEOUT_SECONDS', 0.2)
    holders = []

    def hold(job_id):
        taken, release = threading.Event(), threading.Event()

        def run():
            with jobs._fire_job_lock(job_id) as acquired:
                assert acquired
                taken.set()
                release.wait(10)
        context = contextvars.copy_context()  # the same cron store (use_cron_store is a contextvar)
        thread = threading.Thread(target=context.run, args=(run,), daemon=True)
        thread.start()
        assert taken.wait(5)
        holders.append((release, thread))
        return lambda: (release.set(), thread.join(5))
    yield hold
    for release, thread in holders:
        release.set()
        thread.join(5)


def test_a_refused_booking_keeps_the_journal_and_the_next_tick_books_it_once(
        store, side_effects, busy_fence, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='telegram:123')
    fire, journal = _dead_firer_fire(job)
    claim = _claim_b(job['id'])
    owner = _Owner(monkeypatch, job['id'], [True, 'document', 'answer', None])
    release = busy_fence(job['id'])
    scheduler_authority.reconcile_pending()
    assert journal.exists()
    assert executions.get_execution(fire)['status'] not in executions.FIRER_FINISHED_STATES
    assert jobs.get_job(job['id'])['repeat']['completed'] == 0  # not booked yet
    assert side_effects['saved'] == 1 and len(side_effects['queued']) == 1
    release()
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not journal.exists()
    assert after['repeat']['completed'] == 1 and after['last_status'] == 'delivery_queued'
    assert after['fire_claim'] == claim  # B's claim still stands
    assert executions.get_execution(fire)['status'] == 'completed'
    # Booked from the stamp: no second save or notice, and the owner (which forgot the admission
    # by now) was not asked again, so A was not booked as a missing admission.
    assert side_effects['saved'] == 1 and len(side_effects['queued']) == 1
    assert owner.calls == 1
    scheduler_authority.reconcile_pending()
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1


def test_a_booking_refused_on_every_tick_never_delivers_again(
        store, side_effects, busy_fence, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='telegram:123')
    _fire, journal = _dead_firer_fire(job)
    _claim_b(job['id'])
    _Owner(monkeypatch, job['id'], [False, 'document', '', 'boom'])
    busy_fence(job['id'])
    for _ in range(3):
        scheduler_authority.reconcile_pending()
    assert journal.exists()
    assert side_effects['saved'] == 1 and len(side_effects['queued']) == 1
    assert jobs.get_job(job['id'])['repeat']['completed'] == 0


def test_a_refused_failure_keeps_its_verdict_and_lifts_its_own_pause_once_booked(
        store, side_effects, busy_fence, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    fire, journal = _dead_firer_fire(job, claim={'by': 'tick-A', 'seq': 1})
    scheduler_authority.pause_for_unknown(job['id'], fire, 'Canonical cron unknown')
    assert jobs.get_job(job['id'])['state'] == 'paused'
    _Owner(monkeypatch, job['id'], [False, 'document', '', 'boom'])
    release = busy_fence(job['id'])
    scheduler_authority.reconcile_pending()
    assert journal.exists() and jobs.get_job(job['id'])['state'] == 'paused'
    release()
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not journal.exists()
    assert after['state'] == 'scheduled' and after['last_status'] == 'error'
    assert after['failure_streak'] == 1 and after['repeat']['completed'] == 1
    assert executions.get_execution(fire)['status'] == 'failed'


def test_a_refused_booking_of_a_removed_job_is_finalized(store, side_effects, busy_fence, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    fire, journal = _dead_firer_fire(job)
    _Owner(monkeypatch, job['id'], [True, 'document', 'answer', None])
    real_mark = scheduler.mark_job_run

    def mark_after_removal(job_id, *args, **kwargs):
        jobs.remove_job(job_id)
        return real_mark(job_id, *args, **kwargs)
    monkeypatch.setattr(scheduler, 'mark_job_run', mark_after_removal)
    scheduler_authority.reconcile_pending()
    assert not journal.exists()
    assert executions.get_execution(fire)['status'] == 'completed'


def test_a_stamped_retry_holds_only_what_is_left_of_the_quota_window(store, monkeypatch):
    from datetime import timedelta

    from cron.quota_hold import HOLD_FROM_KEY
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    delivery = scheduler._RunDelivery(dict(job, _quota_hold_seconds=3600.0), False, 'quota')
    aged_at = (jobs._hermes_now() - timedelta(minutes=20)).isoformat()  # aged before a slow save
    stamp = scheduler_authority._booking_stamp(delivery, {'by': 'tick-A', 'seq': 1}, aged_at)
    rebuilt = scheduler_authority._from_booking_stamp(stamp)
    assert 2390 < rebuilt.job['_quota_hold_seconds'] < 2410
    assert HOLD_FROM_KEY not in rebuilt.job
    assert rebuilt.success is False and rebuilt.error == 'quota'


def test_the_notice_announces_no_quota_hold_the_booking_leaves_to_a_newer_fire(
        store, side_effects, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='telegram:123')
    _fire, _journal = _dead_firer_fire(job)
    _claim_b(job['id'])
    _Owner(monkeypatch, job['id'], [False, 'document', '', 'usage limit reached'],
           _quota_hold_seconds=3600)
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not after.get('quota_hold_until')
    assert len(side_effects['queued']) == 1
    assert "usage window is closed" not in side_effects['queued'][0][1]


def test_the_notice_still_announces_the_hold_it_installs_without_a_newer_fire(
        store, side_effects, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='telegram:123')
    _fire, _journal = _dead_firer_fire(job)
    _Owner(monkeypatch, job['id'], [False, 'document', '', 'usage limit reached'],
           _quota_hold_seconds=3600)
    scheduler_authority.reconcile_pending()
    assert jobs.get_job(job['id']).get('quota_hold_until')
    assert "usage window is closed" in side_effects['queued'][0][1]


def test_an_unreachable_failure_under_a_newer_fire_alerts_instead_of_waiting_for_a_rerun(
        store, side_effects, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1d', deliver='telegram:123')
    _fire, _journal = _dead_firer_fire(job)
    _claim_b(job['id'])
    _Owner(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'],
           _model_unreachable=True)
    scheduler_authority.reconcile_pending()
    assert 'unreachable_retry' not in jobs.get_job(job['id'])
    assert len(side_effects['queued']) == 1  # no ladder re-run will deliver it later


def test_the_stamp_stores_sanitized_errors_and_no_stale_receipt(store):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    stale = {'canonical': {'status': 'pending', 'execution_id': 'older'}}
    delivery = scheduler._RunDelivery(dict(job, last_delivery_queued=stale), False,
                                      'fetch https://user:hunter2@example.com/x failed')
    stamp = scheduler_authority._booking_stamp(delivery, None, jobs._hermes_now().isoformat())
    assert 'hunter2' not in json.dumps(stamp)
    # A silent run's first booking clears the stale receipt; its retry must not clear a newer one.
    assert stamp['job']['last_delivery_queued'] is None


def test_a_deferred_booking_of_a_job_recreated_under_the_same_id_settles_the_ledger_only(
        store, side_effects, busy_fence, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local', repeat=1)
    fire, journal = _dead_firer_fire(job)
    _Owner(monkeypatch, job['id'], [True, 'document', 'answer', None])
    release = busy_fence(job['id'])
    scheduler_authority.reconcile_pending()
    release()
    assert journal.exists()
    original = jobs.get_job(job['id'])
    jobs.remove_job(job['id'])
    with jobs._jobs_lock():  # a definition re-import: a fresh record under the same stable id
        stored = jobs.load_jobs()
        stored.append(dict(original, created_at=jobs._hermes_now().isoformat(),
                           state='paused', enabled=False))
        jobs.save_jobs(stored)
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not journal.exists()
    assert after['repeat']['completed'] == 0 and after['state'] == 'paused'
    assert executions.get_execution(fire)['status'] == 'completed'


def test_a_send_drained_while_the_booking_waited_is_booked_with_its_verdict(
        store, side_effects, busy_fence, monkeypatch):
    from cron import delivery_queue
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='telegram:123')
    fire, _journal = _dead_firer_fire(job)
    _claim_b(job['id'])
    _Owner(monkeypatch, job['id'], [True, 'document', 'answer', None])
    release = busy_fence(job['id'])
    scheduler_authority.reconcile_pending()
    release()
    executions.create_execution(job['id'], source='test')  # B's newer ledger attempt
    monkeypatch.setattr(delivery_queue, 'get_status',
                        lambda eid: {'execution_id': eid, 'status': 'delivered'})
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['repeat']['completed'] == 1 and after['last_status'] == 'ok'
    assert executions.get_execution(fire)['delivery_outcome'] == 'delivered'


def test_a_replayed_booking_previews_no_skipped_plan(store):
    from cron import jobs_newer_fire
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    claim = _claim_b(job['id'])
    stored = dict(jobs.get_job(job['id']), canonical_completions=['fire-a'])
    assert not jobs_newer_fire.planners_skipped(stored, 'fire-a', {'by': 'tick-A', 'seq': 1})
    assert jobs_newer_fire.planners_skipped(stored, 'fire-x', {'by': 'tick-A', 'seq': 1})
    assert stored['fire_claim'] == claim  # read-only
