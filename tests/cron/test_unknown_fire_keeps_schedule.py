"""An unknown receipt settles one fire, not the recurring schedule that produced it."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json

import pytest

from cron import executions, jobs, scheduler, scheduler_authority as authority
from cron.scheduler_worker_failure import record_unknown_worker_outcome
from hermes_cli import gateway_client
from tests.cron.test_observer_reconnects_by_receipt import RECEIPT
from tests.cron.test_parked_unknown_notice import _notices, _peer, _unknown_fire


@pytest.fixture(autouse=True)
def clock(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 26, 13, 0, tzinfo=UTC)]
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now[0])
    _peer(monkeypatch, lambda method: {'status': 'unknown'} if method == 'cron.recover' else pytest.fail(method))
    with jobs.use_cron_store(tmp_path / 'cron'):
        yield now


def _claim(schedule='every 1h', *, manual=False):
    job = jobs.create_job(prompt='morning brief', schedule=schedule, deliver='local')
    updates = {'failure_streak': 2}
    if not manual:
        updates['next_run_at'] = jobs._hermes_now().isoformat()
    jobs.update_job(job['id'], updates)
    claimed = jobs.claim_job_for_fire(job['id'], manual=manual, return_job=True)
    claimed['execution_id'] = executions.create_execution(job['id'], source='test')['id']
    return claimed


def _provisional(monkeypatch, job):
    journal = _unknown_fire(monkeypatch, job, job['execution_id'])
    assert record_unknown_worker_outcome(job)
    return journal


def _assert_schedule_kept(job, notices):
    saved = jobs.get_job(job['id'])
    assert (saved['state'], saved['enabled'], saved['paused_at'], saved['paused_reason']) == (
        'scheduled', True, None, None)
    assert saved['next_run_at'] == job['next_run_at']
    assert datetime.fromisoformat(saved['next_run_at']) > jobs._hermes_now()
    assert saved['last_status'] == 'unknown' and saved['failure_streak'] == job['failure_streak']
    assert saved['last_error'] == 'Canonical cron unknown; no automatic re-execution'
    assert saved['last_failure'] == {'at': saved['last_run_at'], 'detail': saved['last_error']}
    assert not {'unreachable_retry', 'quota_hold_until', 'quota_hold_cron_expr'}.intersection(saved)
    assert saved['repeat']['completed'] == 1
    assert job['execution_id'] in saved['canonical_completions']
    assert len(notices) == 1 and notices[0][0] == job['id']
    assert notices[0][1].splitlines()[-1] == (
        f"Outcome unknown (no automatic re-run); schedule kept, next run {saved['next_run_at']}")


def test_recurring_unknown_fire_keeps_its_schedule(monkeypatch, clock):
    delivered = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda job, text, **kw: delivered.append((job['id'], text)))
    job = _claim()
    journal = _provisional(monkeypatch, job)
    marked = jobs.get_job(job['id'])
    assert marked['state'] == 'paused' and marked['paused_at'] == json.loads(journal.read_text())['pause_recorded']['paused_at']
    assert marked['failure_streak'] == 3 and marked['next_run_at'] == job['next_run_at']
    assert delivered == []
    clock[0] += timedelta(minutes=10)
    authority.reconcile_pending()
    _assert_schedule_kept(job, delivered)


@pytest.mark.skipif(not jobs._ensure_croniter(), reason='hourly cron expression requires croniter')
def test_replay_26_nov_drop_then_restart_turn_lost(monkeypatch, clock):
    notices = _notices(monkeypatch)
    job = _claim('0 * * * *')
    journal = authority.journal_path(job['id'], job['execution_id'])
    elapsed, calls, dials = 0, [], []

    async def sleep(delay):
        nonlocal elapsed
        elapsed += delay
        clock[0] += timedelta(seconds=delay)

    class Peer:
        async def rpc(self, method, **params):
            calls.append((method, params))
            if method == 'cron.submit':
                return RECEIPT
            if method == 'cron.status' and not elapsed:
                raise gateway_client.GatewayClientError(gateway_client.DISCONNECTED)
            return {'status': 'unknown'}

    @asynccontextmanager
    async def connect():
        dials.append(elapsed)
        if 0 < elapsed < 330:
            raise ConnectionRefusedError('gateway restarting')
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    monkeypatch.setattr(authority.time, 'monotonic', lambda: elapsed)
    monkeypatch.setattr(authority.asyncio, 'sleep', sleep)
    monkeypatch.setattr(authority, 'RECONNECT_BUDGET_S', 600)
    monkeypatch.setattr(authority, 'RECONNECT_FIRST_DELAY_S', 30)
    with pytest.raises(authority.CronExecutionUnknown, match='unknown_execution') as lost:
        authority.run_canonical_job(job)
    assert elapsed == 330 and len(dials) > 2
    assert json.loads(journal.read_text())['receipt'] == RECEIPT
    authority.pause_for_unknown(job['id'], job['execution_id'], str(lost.value))
    assert executions.give_up_execution(job['execution_id'], reason=str(lost.value))
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert record_unknown_worker_outcome(job) and not notices
    authority.reconcile_pending()
    assert [method for method, _ in calls] == ['cron.submit', 'cron.status', 'cron.status', 'cron.recover']
    assert calls[1][1] == calls[2][1] == RECEIPT
    _assert_schedule_kept(job, notices)


def test_one_shot_unknown_fire_still_pauses(monkeypatch):
    notices = _notices(monkeypatch)
    job = _claim('in 1h', manual=True)
    _provisional(monkeypatch, job)
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert saved['state'] == 'paused' and saved['enabled'] is False
    assert len(notices) == 1 and 'schedule kept' not in notices[0][1]


def test_pause_on_unknown_opt_in_still_pauses(monkeypatch):
    notices = _notices(monkeypatch)
    job = _claim()
    jobs.update_job(job['id'], {'pause_on_unknown': True})
    journal = _provisional(monkeypatch, job)
    authority.reconcile_pending()
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert saved['state'] == 'paused' and saved['enabled'] is False
    assert len(notices) == 1 and 'schedule kept' not in notices[0][1]
    assert json.loads(journal.read_text())['unknown_settled'] is True


def test_operator_pause_before_the_fire_is_untouched(monkeypatch, clock):
    _notices(monkeypatch)
    job = _claim()
    held = jobs.pause_job(job['id'], reason='operator maintenance')
    clock[0] += timedelta(seconds=10)
    _provisional(monkeypatch, job)
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert saved['state'] == 'paused' and saved['enabled'] is False
    assert (saved['paused_at'], saved['paused_reason']) == (held['paused_at'], held['paused_reason'])
    assert saved['last_status'] == 'unknown' and saved['failure_streak'] == 2


def test_terminal_receipt_after_temporary_pause_lifts_it(monkeypatch):
    notices = _notices(monkeypatch)
    job = _claim()
    journal = _provisional(monkeypatch, job)
    assert jobs.get_job(job['id'])['state'] == 'paused'
    _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': job, 'result': [True, 'brief', 'answer', None]})
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert (saved['state'], saved['enabled'], saved['paused_at'], saved['paused_reason']) == ('scheduled', True, None, None)
    assert saved['last_status'] == 'ok' and saved['repeat']['completed'] == 1
    assert not journal.exists() and not notices


def test_settle_is_idempotent_across_ticks(monkeypatch, clock):
    notices = _notices(monkeypatch)
    job = _claim()
    journal = _provisional(monkeypatch, job)
    authority.reconcile_pending()
    first = jobs.get_job(job['id'])
    for _ in range(2):
        clock[0] += timedelta(minutes=1)
        authority.reconcile_pending()
    assert jobs.get_job(job['id'])['last_run_at'] == first['last_run_at']
    assert json.loads(journal.read_text())['unknown_settled'] is True
    _assert_schedule_kept(job, notices)


def test_refused_settlement_retries_next_tick_and_notice_still_goes_once(monkeypatch):
    notices = _notices(monkeypatch)
    job = _claim()
    journal = _provisional(monkeypatch, job)
    with monkeypatch.context() as patch:
        patch.setattr(jobs, 'mark_job_run', lambda *args, **kwargs: False)
        authority.reconcile_pending()
    assert not json.loads(journal.read_text()).get('unknown_settled')
    assert jobs.get_job(job['id'])['state'] == 'paused'
    assert len(notices) == 1 and 'schedule kept' not in notices[0][1]
    authority.reconcile_pending()
    assert json.loads(journal.read_text())['unknown_settled'] is True
    saved = jobs.get_job(job['id'])
    assert saved['last_status'] == 'unknown' and saved['state'] == 'scheduled'
    assert saved['repeat']['completed'] == 1 and len(notices) == 1


@pytest.mark.parametrize('has_journal', [True, False])
def test_no_recovery_path_does_not_pause_a_recurring_job(monkeypatch, has_journal):
    monkeypatch.setattr(authority, '_builtin_ticker', lambda: not has_journal)
    for schedule, state in [('in 1h', 'paused'), ('every 1h', 'scheduled')]:
        job = _claim(schedule)
        if has_journal:
            journal = authority.journal_path(job['id'], job['execution_id'])
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text('{}', encoding='utf-8')
        authority.pause_for_unknown(job['id'], job['execution_id'], 'disconnected')
        saved = jobs.get_job(job['id'])
        assert saved['state'] == state
        assert saved['fire_claim'] == job['fire_claim'] and saved['last_run_at'] is None


@pytest.mark.parametrize('newer_success', [True, False])
def test_historical_unknown_corrects_only_its_provisional_failure(monkeypatch, clock, newer_success):
    _notices(monkeypatch)
    job = _claim()
    _provisional(monkeypatch, job)
    clock[0] += timedelta(minutes=1)
    assert jobs.mark_job_run(job['id'], newer_success, error=None if newer_success else 'newer failure')
    newer = jobs.get_job(job['id'])
    authority.reconcile_pending()
    saved = jobs.get_job(job['id'])
    assert saved['last_status'] == newer['last_status'] and saved['last_run_at'] == newer['last_run_at']
    assert saved['repeat']['completed'] == 2 and saved['next_run_at'] == newer['next_run_at']
    assert saved['failure_streak'] == (0 if newer_success else 3)
    assert saved['last_failure']['detail'] == (
        'Canonical cron unknown; no automatic re-execution' if newer_success else 'newer failure')


@pytest.mark.parametrize('removed', [True, False])
def test_retired_one_shot_still_sends_its_parked_notice(monkeypatch, removed):
    notices = _notices(monkeypatch)
    job = _claim('in 1h')
    journal = authority.journal_path(job['id'], job['execution_id'])
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({'params': {'job_id': job['id'], 'request_id': job['execution_id']},
                                   'receipt': RECEIPT}), encoding='utf-8')
    assert executions.give_up_execution(job['execution_id'], reason='worker died')
    assert record_unknown_worker_outcome(job)
    assert jobs.get_job(job['id'])['state'] == 'completed'
    if removed:
        assert jobs.remove_job(job['id'])
    authority.reconcile_pending()
    authority.reconcile_pending()
    assert notices == [(job['id'], 'worker died')]
    assert json.loads(journal.read_text())['unknown_settled'] is True
