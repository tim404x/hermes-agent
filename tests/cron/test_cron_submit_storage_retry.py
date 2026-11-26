"""A storage refusal is retryable only while the owner proves this fire was not admitted."""
import asyncio
from datetime import UTC, datetime, timedelta
import json
import logging
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from cron import executions, jobs, scheduler, scheduler_authority, unreachable_retry
from gateway import session_cron
from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError
from persistence.core.errors import (
    AuthorityRefusal, DiskFull, PermanentStoreError, StoreContention, StoreReplaced, StoreUnavailable,
)
from persistence.sqlite._state.write import WriterMutexTimeout
from tests.cron.test_observer_reconnects_by_receipt import RECEIPT, _gateway
from tests.cron.test_recovery_keeps_newer_fire import _claim_b


RESULT = (True, 'out', 'answer', None)
TERMINAL = {'status': 'terminal', 'result': list(RESULT)}
MISSING = {'status': 'missing', 'result': None}
LOCKED = 'database is locked (in-process state.db writer mutex exceeded write patience)'


def _refusal(**data):
    return GatewayRPCError('storage_unavailable', 5001, {'reason': 'storage_unavailable', **data})


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(scheduler_authority, 'STORAGE_RETRY_DELAYS_S', (0.001,) * 4, raising=False)


def _record(job):
    journal = scheduler_authority.journal_path(job['id'], job['execution_id'])
    assert list(journal.parent.glob('*.json')) == [journal]
    return json.loads(journal.read_text())


@pytest.mark.parametrize('refusal', [
    _refusal(), GatewayRPCError('request_failed', 5001, {'reason': 'storage_unavailable', 'transient': True}),
    GatewayRPCError('storage_unavailable', 5001),
])
def test_remote_reuses_fire_and_journal_after_missing(monkeypatch, caplog, refusal):
    job = {'id': 'hourly', 'execution_id': 'fire'}
    seen = _gateway(monkeypatch, [{'submit': [refusal, RECEIPT], 'recover': [MISSING], 'status': [TERMINAL]}])
    with caplog.at_level(logging.INFO, logger=scheduler_authority.__name__):
        assert scheduler_authority.run_canonical_job(job, extra_prompt='unchanged') == RESULT
    params = {'job_id': 'hourly', 'request_id': 'fire', 'extra_prompt': 'unchanged'}
    assert seen == [[('submit', params), ('recover', params), ('submit', params), ('status', RECEIPT)]]
    record = _record(job)
    assert record['params'] == params and record['receipt'] == RECEIPT and 'refusal' not in record
    assert sum('resubmitting execution fire' in message for message in caplog.messages) == 1


@pytest.mark.parametrize(('refusal', 'retry'), [
    (StoreContention(LOCKED), True), (WriterMutexTimeout(LOCKED), True),
    (sqlite3.OperationalError('BUSY'), True), (DiskFull('full'), True),
    (PermanentStoreError('locked'), False), (StoreUnavailable('busy'), False),
    (StoreReplaced('locked'), False), (AuthorityRefusal('storage_unavailable'), False),
    (sqlite3.OperationalError('no such table'), False),
])
@pytest.mark.asyncio
async def test_owner_uses_typed_refusal_and_same_params(monkeypatch, refusal, retry):
    job = {'id': 'hourly', 'execution_id': 'fire'}
    params_seen = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: (object(), loop))

    async def operation(owner, method, params):
        assert asyncio.get_running_loop() is loop
        if method == 'recover':
            return MISSING
        if method == 'status':
            return TERMINAL
        assert method == 'submit'
        params_seen.append(params)
        if len(params_seen) == 1:
            raise refusal
        return RECEIPT

    monkeypatch.setattr(session_cron, 'operation', operation)
    result = await asyncio.to_thread(scheduler_authority.run_canonical_job, job)
    if retry:
        assert result == RESULT
        assert len(params_seen) == 2 and params_seen[0] is params_seen[1]
        assert 'refusal' not in _record(job)
    else:
        assert len(params_seen) == 1 and not result[0] and str(refusal) in result[3]
        assert '_model_unreachable' not in job and '_model_unreachable' not in _record(job)['job']


@pytest.mark.parametrize('refusal', [GatewayRPCError('invalid_params', 4001, {'reason': 'invalid_params'}),
                                    _refusal(transient=False)])
def test_remote_permanent_refusal_is_booked_immediately(monkeypatch, refusal):
    job = {'id': 'hourly', 'execution_id': 'fire'}
    seen = _gateway(monkeypatch, [{'submit': [refusal], 'recover': [MISSING]}])
    result = scheduler_authority.run_canonical_job(job)
    assert not result[0] and result[3] == f'GatewayRPCError: {refusal}'
    assert [op for op, _ in seen[0]] == ['submit', 'recover']
    assert '_model_unreachable' not in job and '_model_unreachable' not in _record(job)['job']


@pytest.mark.parametrize('read_failures', [0, 2])
def test_persistent_contention_shares_one_budget_and_journals_unreachable(monkeypatch, read_failures):
    job = {'id': 'hourly', 'execution_id': 'fire'}
    seen = _gateway(monkeypatch, [{'submit': [_refusal() for _ in range(5)],
                                   'recover': [_refusal() for _ in range(read_failures)] + [MISSING] * 5}])
    result = scheduler_authority.run_canonical_job(job)
    resubmits = 4 - read_failures
    assert [op for op, _ in seen[0]].count('submit') == 1 + resubmits
    assert not result[0] and result[3] == ('GatewayRPCError: storage_unavailable '
                                          f'(transient storage contention persisted through {resubmits} resubmits)')
    assert job['_model_unreachable'] is True
    record = _record(job)
    assert record['job']['_model_unreachable'] is True and record['refusal'] == list(result)


@pytest.mark.parametrize('newer_fire', [False, True], ids=['live-tail', 'recover-under-newer-claim'])
def test_exhausted_fire_books_once_and_only_suppresses_a_planned_rerun(tmp_path, monkeypatch, caplog, newer_fire):
    _gateway(monkeypatch, [{'submit': [_refusal() for _ in range(5)], 'recover': [MISSING] * 5}])
    now = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now)
    monkeypatch.setattr(unreachable_retry, '_hermes_now', lambda: now)
    with jobs.use_cron_store(tmp_path / 'cron'), caplog.at_level(logging.INFO, logger='cron.scheduler'):
        job = jobs.create_job('report', 'every 1h', deliver='telegram:123')
        fire = executions.create_execution(job['id'], source='test')['id']
        job['execution_id'] = fire
        assert unreachable_retry.will_retry(job)
        if newer_fire:
            job['fire_claim'] = {'by': 'tick-A', 'seq': 1}
            assert not scheduler_authority.run_canonical_job(job)[0]
            assert _record(job)['job']['_model_unreachable'] is True
            assert executions.give_up_execution(fire, reason='firer exited before booking')
            claim = _claim_b(job['id'])
            scheduler_authority.reconcile_pending(allow_connect=False)
        else:
            scheduler._run_one_job_body(job)
        saved = jobs.get_job(job['id'])
        assert saved['repeat']['completed'] == 1
        row = executions.get_execution(fire)
        if newer_fire:
            assert saved['fire_claim'] == claim and 'unreachable_retry' not in saved
            assert row['delivery_outcome'] == 'queued'
        else:
            assert datetime.fromisoformat(saved['next_run_at']) == now + timedelta(minutes=5)
            assert row['delivery_outcome'] == 'suppressed'
            assert 'suppressing failure notice' in caplog.text


@pytest.mark.parametrize('persistent', [False, True])
def test_refused_recover_retries_until_admission_is_known_or_budget_expires(monkeypatch, persistent):
    reads = [_refusal() for _ in range(5)] if persistent else [_refusal(), MISSING]
    seen = _gateway(monkeypatch, [{'submit': [_refusal(), RECEIPT], 'recover': reads, 'status': [TERMINAL]}])
    job = jobs.create_job('report', 'every 1h', deliver='local')
    job['execution_id'] = 'fire'
    if persistent:
        with pytest.raises(scheduler_authority.CronExecutionUnknown, match='storage_unavailable'):
            scheduler_authority.run_canonical_job(job)
        assert [op for op, _ in seen[0]] == ['submit'] + ['recover'] * 5
        assert 'refusal' not in _record(job)
    else:
        job['execution_id'] = executions.create_execution(job['id'], source='test')['id']
        assert scheduler._run_one_job_body(job)
        saved = jobs.get_job(job['id'])
        assert saved['last_status'] == 'ok' and saved['state'] != 'paused'
        assert [op for op, _ in seen[0]] == ['submit', 'recover', 'recover', 'submit', 'status']


@pytest.mark.parametrize('read', [MISSING, _refusal()], ids=['resubmit-backoff', 'recover-backoff'])
def test_cancel_during_backoff_books_stop_without_resubmitting(monkeypatch, read):
    job = {'id': 'hourly', 'name': 'report', 'execution_id': 'fire'}
    cancel = threading.Event()
    cancel.reason = 'operator cancelled'
    seen = _gateway(monkeypatch, [{'submit': [_refusal()], 'recover': [read]}])
    monkeypatch.setattr(scheduler_authority, 'STORAGE_RETRY_DELAYS_S', (8.0,))

    async def cancel_during_sleep(delay):
        assert 0 < delay <= 0.5
        cancel.set()

    monkeypatch.setattr(asyncio, 'sleep', cancel_during_sleep)
    result = scheduler_authority.run_canonical_job(job, cancel_event=cancel)
    assert not result[0] and result[3] == "RuntimeError: Cron job 'report' was stopped: operator cancelled"
    assert [op for op, _ in seen[0]] == ['submit', 'recover']
    assert '_model_unreachable' not in job and '_model_unreachable' not in _record(job)['job']


def test_a_disconnected_resubmit_is_not_a_definite_refusal(monkeypatch):
    seen = _gateway(monkeypatch, [{'submit': [_refusal(), GatewayClientError('disconnected')], 'recover': [MISSING]},
                                  {'recover': [MISSING]}])
    job = {'id': 'hourly', 'execution_id': 'fire'}
    with pytest.raises(scheduler_authority.CronExecutionUnknown, match='missing'):
        scheduler_authority.run_canonical_job(job)
    assert [op for op, _ in seen[1]] == ['recover'] and 'refusal' not in _record(job)


@pytest.mark.parametrize('persistent', [False, True])
def test_poll_retries_only_consecutive_transient_status_failures(monkeypatch, caplog, persistent):
    reads = ([_refusal()] * 3 if persistent else
             [_refusal(), _refusal(), {'status': 'started'}, _refusal(), _refusal(), TERMINAL])
    seen = _gateway(monkeypatch, [{'submit': [RECEIPT], 'status': reads}])
    now = 0.0

    async def tick(delay):
        nonlocal now
        now += 60.0

    monkeypatch.setattr(asyncio, 'sleep', tick)
    monkeypatch.setattr(scheduler_authority, 'time', SimpleNamespace(monotonic=lambda: now))
    job = {'id': 'hourly', 'execution_id': 'fire'}
    with caplog.at_level(logging.INFO, logger=scheduler_authority.__name__):
        if persistent:
            with pytest.raises(scheduler_authority.CronExecutionUnknown, match='storage_unavailable'):
                scheduler_authority.run_canonical_job(job)
            assert now == 120.0
        else:
            assert scheduler_authority.run_canonical_job(job) == RESULT
    assert len(seen) == 1 and _record(job)['receipt'] == RECEIPT
    assert len([record for record in caplog.records if record.name == scheduler_authority.__name__
                and record.levelno == logging.INFO]) == (1 if persistent else 2)


def test_transient_cancel_does_not_hide_status_or_lose_stop(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    seen = _gateway(monkeypatch, [{'submit': [RECEIPT], 'status': [{'status': 'started'}, TERMINAL],
                                   'cancel': [_refusal(), {}]}])
    assert scheduler_authority.run_canonical_job({'id': 'hourly'}, cancel_event=cancel) == RESULT
    assert [op for op, _ in seen[0]] == ['submit', 'cancel', 'status', 'cancel', 'status']
