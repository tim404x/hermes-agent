"""Receipt recovery lifts the pause its own unknown fire caused without recomputing the schedule.

GPT 6.2 review of t_e473dd07 (24 Nov 2026): recovery booked the real outcome while the job was still
held by that pause, so the unreachable-retry ladder and the quota hold refused to plan (both skip a
paused job), and the lift that followed went through ``resume_job``, which recomputed ``next_run_at``
from now: a recovered unreachable daily run scheduled no retry, a one-minute job announced a one-hour
quota hold yet was due the next minute, and a manual run's kept occurrence moved an hour. The lift now
happens inside the same ``mark_job_run`` write, before the planners, and keeps the booked instant.
"""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json

import pytest

from cron import executions, jobs, quota_hold, scheduler_authority, unreachable_retry
from cron.extra_fire import _CLAIM_KEY
from hermes_cli import gateway_client
from tests.cron.test_parked_unknown_notice import (
    _notices, _peer, _repeat_limited_unknown_fire, _unknown_fire, _waiter_marks_unknown)


def _terminal(monkeypatch, job_id, result, **flags):
    _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': jobs.get_job(job_id),
                                       'result': result, 'job_flags': flags})


def _at(value):
    return jobs._parse_aware(value)


def _paused_unknown_fire(monkeypatch, schedule):
    _notices(monkeypatch)
    job = jobs.create_job(prompt='brief', schedule=schedule, deliver='local')
    fire = executions.create_execution(job['id'], source='test')['id']
    journal = _unknown_fire(monkeypatch, job, fire)
    _waiter_marks_unknown(monkeypatch, job, fire)
    assert jobs.get_job(job['id'])['state'] == 'paused'
    return job, fire, journal


def test_a_recovered_unreachable_run_schedules_its_retry(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, journal = _paused_unknown_fire(monkeypatch, 'every 1d')
        _terminal(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'],
                  _model_unreachable=True)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert not journal.exists() and after['state'] == 'scheduled' and after['enabled'] is True
        assert after['unreachable_retry']['attempt'] == 1
        assert after['next_run_at'] == after['unreachable_retry']['at']
        assert _at(after['next_run_at']) - jobs._hermes_now() < timedelta(minutes=6)


def test_a_recovered_quota_failure_holds_for_the_window_it_reports(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _paused_unknown_fire(monkeypatch, 'every 1m')
        _terminal(monkeypatch, job['id'], [False, 'document', '', '429 quota'],
                  _quota_hold_seconds=3600)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['state'] == 'scheduled' and after.get('quota_hold_until')
        assert _at(after['next_run_at']) - jobs._hermes_now() > timedelta(minutes=55)


def test_a_recovered_manual_run_keeps_the_occurrence_its_mark_kept(tmp_path, monkeypatch):
    """An hourly job due at :00 fired by hand at :50: the kept :00 slot must not move to :53+1h."""
    _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        due = (jobs._hermes_now() + timedelta(minutes=10)).isoformat()
        jobs.update_job(job['id'], {'next_run_at': due})
        fire = executions.create_execution(job['id'], source='test')['id']
        _unknown_fire(monkeypatch, job, fire)
        jobs.update_job(job['id'], {'fire_claim': {'by': 'gateway', _CLAIM_KEY: True}})
        from cron.scheduler_worker_failure import record_unknown_worker_outcome
        from contextlib import contextmanager
        from cron import scheduler

        @contextmanager
        def owns(job_id, expected_owner):
            yield True
        monkeypatch.setattr(scheduler, 'fire_claim_fence', owns)
        assert record_unknown_worker_outcome(dict(jobs.get_job(job['id']), execution_id=fire))
        assert jobs.get_job(job['id'])['next_run_at'] == due  # the provisional mark kept it
        _terminal(monkeypatch, job['id'], [True, 'document', 'answer', None])
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['state'] == 'scheduled' and after['next_run_at'] == due


def test_an_operator_pause_is_never_lifted_and_keeps_its_schedule(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, journal = _paused_unknown_fire(monkeypatch, 'every 1h')
        jobs.resume_job(job['id'])
        jobs.pause_job(job['id'], reason='operator hold')
        _terminal(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'],
                  _model_unreachable=True)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert not journal.exists()
        assert (after['state'], after['paused_reason']) == ('paused', 'operator hold')
        assert 'unreachable_retry' not in after  # a paused job plans no ladder


def test_a_replayed_receipt_still_lifts_the_pause(tmp_path, monkeypatch):
    """The first booking landed but the journal survived (a crash before its unlink): the replay is
    a no-op for the outcome and still lifts the pause."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire, _journal = _paused_unknown_fire(monkeypatch, 'every 1h')
        pause = jobs.get_job(job['id'])
        identity = {'paused_at': pause['paused_at'], 'reason': pause['paused_reason']}
        assert jobs.mark_job_run(job['id'], True, execution_id=fire)
        jobs.update_job(job['id'], {'state': 'paused', 'enabled': False,
                                    'paused_at': identity['paused_at'],
                                    'paused_reason': identity['reason']})
        before = jobs.get_job(job['id'])['next_run_at']
        assert jobs.mark_job_run(job['id'], True, execution_id=fire, lift_pause=identity)
        after = jobs.get_job(job['id'])
        assert after['state'] == 'scheduled' and after['next_run_at'] == before


def test_a_recovered_success_after_a_newer_failure_drops_its_own_streak_share(tmp_path, monkeypatch):
    """Fire B booked between A's provisional mark and A's recovery: A's real success must not leave
    A's +1 in the streak (fire order: A ok, B failed = streak 1)."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _repeat_limited_unknown_fire(tmp_path, monkeypatch)
        newer = executions.create_execution(job['id'], source='test')['id']
        jobs.update_job(job['id'], {'fire_claim': {'by': 'next-fire'}})
        assert jobs.mark_job_run(job['id'], False, 'newer broke', execution_id=newer,
                                 expected_fire_owner='next-fire')
        assert jobs.get_job(job['id'])['failure_streak'] == 2
        _terminal(monkeypatch, job['id'], [True, 'document', 'answer', None])
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['failure_streak'] == 1 and 'newer broke' in after['last_failure']['detail']
        assert after['last_error'] == 'newer broke'


def test_a_recovered_success_after_a_newer_success_drops_its_failure_stamp(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _repeat_limited_unknown_fire(tmp_path, monkeypatch)
        assert jobs.get_job(job['id'])['last_failure']
        newer = executions.create_execution(job['id'], source='test')['id']
        jobs.update_job(job['id'], {'fire_claim': {'by': 'next-fire'}})
        assert jobs.mark_job_run(job['id'], True, execution_id=newer, expected_fire_owner='next-fire')
        _terminal(monkeypatch, job['id'], [True, 'document', 'answer', None])
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['failure_streak'] == 0 and after.get('last_failure') is None


def test_a_recovered_failure_after_a_newer_success_names_its_real_error(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _repeat_limited_unknown_fire(tmp_path, monkeypatch)
        newer = executions.create_execution(job['id'], source='test')['id']
        jobs.update_job(job['id'], {'fire_claim': {'by': 'next-fire'}})
        assert jobs.mark_job_run(job['id'], True, execution_id=newer, expected_fire_owner='next-fire')
        _terminal(monkeypatch, job['id'], [False, 'document', '', 'model said no'])
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['failure_streak'] == 0 and after['last_status'] == 'ok'
        assert 'model said no' in after['last_failure']['detail']


# Review round 1 (GPT 6.2, 24 Nov 2026): keeping the booked slot must not keep a stale one.

@pytest.fixture
def clock(monkeypatch):
    now = [datetime(2026, 11, 1, 10, 0, 0, tzinfo=UTC)]
    for mod in (jobs, quota_hold, unreachable_retry):
        monkeypatch.setattr(mod, '_hermes_now', lambda: now[0])
    return now


def _dead_firer_journal(job_id, fire):
    """A fire whose firer died after journaling it (no pause yet, no provisional mark)."""
    journal = scheduler_authority.journal_path(job_id, fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({'params': {'job_id': job_id, 'request_id': fire,
                                              'extra_prompt': None}, 'receipt': None}))
    executions.give_up_execution(fire, reason='firer died')
    return journal


def test_an_operator_pause_is_never_adopted_by_the_unknown_pass(clock, tmp_path, monkeypatch):
    """The operator paused before recovery saw ``unknown``: recovery must not take that pause over
    (and later lift it, arming a five-minute retry on a paused daily job)."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='daily', schedule='every 1d', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _dead_firer_journal(job['id'], fire)
        jobs.pause_job(job['id'], reason='operator maintenance')
        _peer(monkeypatch, lambda method: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        assert jobs.get_job(job['id'])['paused_reason'] == 'operator maintenance'
        _terminal(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'],
                  _model_unreachable=True)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert not journal.exists()
        assert (after['state'], after['paused_reason']) == ('paused', 'operator maintenance')
        clock[0] += timedelta(minutes=6)
        assert job['id'] not in {j['id'] for j in jobs.get_due_jobs()}


def test_the_firer_never_adopts_an_operator_pause_made_mid_run(tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        jobs.pause_job(job['id'], reason='operator hold')
        fire = executions.create_execution(job['id'], source='test')['id']
        scheduler_authority.pause_for_unknown(job['id'], fire, 'unverified')
        assert jobs.get_job(job['id'])['paused_reason'] == 'operator hold'


def test_a_schedule_edited_while_held_takes_its_next_instant(clock, tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, journal = _paused_unknown_fire(monkeypatch, 'every 1d')
        clock[0] += timedelta(minutes=1)
        assert jobs.update_job(job['id'], {'schedule': 'every 5m'})['state'] == 'paused'
        _terminal(monkeypatch, job['id'], [True, 'document', 'answer', None])
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert not journal.exists() and after['state'] == 'scheduled'
        assert _at(after['next_run_at']) == clock[0] + timedelta(minutes=5)


def test_a_receipt_read_late_holds_only_the_rest_of_the_window(clock, tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _paused_unknown_fire(monkeypatch, 'every 1m')
        seen = clock[0].isoformat()  # the owner saw "retry after 3600s" at 10:00
        clock[0] += timedelta(minutes=45)  # recovery reads the receipt at 10:45
        _terminal(monkeypatch, job['id'], [False, 'document', '', '429 quota'],
                  _quota_hold_seconds=3600, _quota_hold_from=seen)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        held = _at(after['quota_hold_until'])
        assert clock[0] + timedelta(minutes=14) < held < clock[0] + timedelta(minutes=17)
        assert quota_hold.HOLD_FROM_KEY not in after


def test_a_receipt_read_after_the_window_closed_holds_nothing(clock, tmp_path, monkeypatch):
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, _fire, _journal = _paused_unknown_fire(monkeypatch, 'every 1m')
        seen = clock[0].isoformat()
        clock[0] += timedelta(hours=2)
        _terminal(monkeypatch, job['id'], [False, 'document', '', '429 quota'],
                  _quota_hold_seconds=3600, _quota_hold_from=seen)
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['state'] == 'scheduled' and not quota_hold.hold_active(after, now=clock[0])


def test_an_expired_one_shot_stays_paused_like_resume_refuses_it(clock, tmp_path, monkeypatch):
    """A manual run of a one-shot kept its 10:10 slot; recovered at 10:40 the slot is past grace."""
    from cron.scheduler_worker_failure import record_unknown_worker_outcome
    _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        due = (clock[0] + timedelta(minutes=10)).isoformat()
        job = jobs.create_job(prompt='one', schedule=due, deliver='local')
        assert jobs.claim_job_for_fire(job['id'], manual=True, return_job=True)['fire_claim'][_CLAIM_KEY]
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        assert record_unknown_worker_outcome(dict(jobs.get_job(job['id']), execution_id=fire))
        assert jobs.get_job(job['id'])['next_run_at'] == due
        clock[0] += timedelta(minutes=40)
        _terminal(monkeypatch, job['id'], [True, 'document', 'answer', None])
        scheduler_authority.reconcile_pending()
        assert not journal.exists()
        jobs.get_due_jobs()
        stored = jobs.get_job(job['id'])
        assert stored is not None and stored['state'] == 'paused'


def _answers_by_fire(monkeypatch, answers):
    class Peer:
        async def rpc(self, method, **params):
            answer = answers.get(params['request_id'])
            if answer is None:
                return {'status': 'started', 'session_id': 's', 'admission_id': 'a'}
            return {'status': 'terminal', 'job': jobs.get_job(params['job_id']), 'result': answer}

    @asynccontextmanager
    async def connected():
        yield Peer()
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)


def _two_pending_fires(monkeypatch):
    _notices(monkeypatch)
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    fires = []
    for _ in range(2):
        fire = executions.create_execution(job['id'], source='test')['id']
        _unknown_fire(monkeypatch, job, fire)
        _waiter_marks_unknown(monkeypatch, job, fire)
        jobs.resume_job(job['id'])
        fires.append(fire)
    assert jobs.get_job(job['id'])['failure_streak'] == 2
    return job, *fires


OK = [True, 'document', 'answer', None]
BAD = [False, 'document', '', 'model said no']


@pytest.mark.parametrize('order', ['fire_order', 'reverse'])
def test_two_recovered_successes_keep_a_newer_real_failure(order, tmp_path, monkeypatch):
    """Fire order A1 ok, A2 ok, B failed: streak 1, whatever order the receipts are read in."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, a1, a2 = _two_pending_fires(monkeypatch)
        newer = executions.create_execution(job['id'], source='test')['id']
        jobs.update_job(job['id'], {'fire_claim': {'by': 'next-fire'}})
        assert jobs.mark_job_run(job['id'], False, 'newer broke', execution_id=newer,
                                 expected_fire_owner='next-fire')
        first, second = (a1, a2) if order == 'fire_order' else (a2, a1)
        _answers_by_fire(monkeypatch, {first: OK})
        scheduler_authority.reconcile_pending()
        _answers_by_fire(monkeypatch, {first: OK, second: OK})
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['failure_streak'] == 1 and 'newer broke' in after['last_failure']['detail']


def test_an_older_success_then_the_latest_real_failure_counts_one(tmp_path, monkeypatch):
    """Fire order A1 ok, A2 failed: A1 is corrected first, then A2 replaces its own outcome from a
    snapshot that must no longer hold A1's provisional +1."""
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, a1, a2 = _two_pending_fires(monkeypatch)
        _answers_by_fire(monkeypatch, {a1: OK})
        scheduler_authority.reconcile_pending()
        _answers_by_fire(monkeypatch, {a1: OK, a2: BAD})
        scheduler_authority.reconcile_pending()
        after = jobs.get_job(job['id'])
        assert after['failure_streak'] == 1 and 'model said no' in after['last_failure']['detail']
        assert 'provisional_runs' not in after
