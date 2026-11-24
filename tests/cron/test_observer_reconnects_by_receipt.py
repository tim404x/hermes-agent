"""A gateway-run cron fire is observed over one WebSocket for the whole agent turn (30+ min). A
transport drop on that socket (a stalled owner loop failed the ``cron.status`` reply, 24 Nov 2026)
said nothing about the run, yet the firer gave up as ``CronExecutionUnknown`` and paused the job
while the turn finished fine inside the gateway. The receipt is durable and ``status``/``recover``
are idempotent reads: the firer reconnects and keeps observing, never submits again, and fails
closed only once the gateway stays unreachable past the reconnect budget."""
from contextlib import asynccontextmanager
import json

import pytest

from cron import executions, jobs, scheduler_authority
from gateway import session_cron
from hermes_cli import gateway_client
from hermes_cli.gateway_client import DISCONNECTED, GatewayClientError, GatewayRPCError, GatewayUnavailableError

RECEIPT = {'session_id': 'local-session', 'admission_id': 'admission'}
RESULT = [True, 'document', 'answer', None]


def _gateway(monkeypatch, connections):
    """``connections[n]`` serves the n-th dial: a ``{method: [answers...]}`` script (an answer that
    is an exception is raised), or an exception the dial itself raises. Returns the calls made,
    one list per dial."""
    seen = []

    class Peer:
        def __init__(self, script):
            self.script, self.calls = script, []

        async def rpc(self, method, **params):
            method = method.removeprefix('cron.')
            self.calls.append((method, params))
            answer = self.script[method].pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    @asynccontextmanager
    async def connected():
        script = connections[len(seen)] if len(seen) < len(connections) else connections[-1]
        peer = Peer(script if isinstance(script, dict) else {})
        seen.append(peer.calls)
        if isinstance(script, BaseException):
            raise script
        yield peer

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    return seen


def _fire(tmp_path):
    job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
    fire = executions.create_execution(job['id'], source='test')['id']
    return job, fire


def _methods(calls):
    return [method for method, _ in calls]


def _started():
    return {'status': 'started', 'result': None, 'job_flags': {}, 'progress_at': None}


def _terminal():
    return {'status': 'terminal', 'result': RESULT, 'job_flags': {}, 'progress_at': None}


@pytest.mark.parametrize('drop', [GatewayClientError(DISCONNECTED), TimeoutError()],
                         ids=['disconnected', 'reply-timeout'])
def test_drop_after_receipt_reconnects_and_reads_the_same_receipt(tmp_path, monkeypatch, drop):
    """The 3rd ``status`` poll loses the socket; a fresh connection reports the run terminal."""
    seen = _gateway(monkeypatch, [
        {'submit': [dict(RECEIPT)], 'status': [_started(), _started(), drop]},
        {'status': [_terminal()]},
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        assert scheduler_authority.run_canonical_job(dict(job), execution_id=fire) == tuple(RESULT)
    assert len(seen) == 2
    assert _methods(seen[0]) == ['submit', 'status', 'status', 'status']
    # Never submitted again: the second connection only reads the saved receipt.
    assert seen[1] == [('status', RECEIPT)]


def test_drop_before_the_receipt_recovers_instead_of_resubmitting(tmp_path, monkeypatch):
    """The ``submit`` answer is lost with the socket: the admission is read back by ``recover``."""
    recovered = dict(_started(), job={}, **RECEIPT)
    seen = _gateway(monkeypatch, [
        {'submit': [GatewayClientError(DISCONNECTED)]},
        {'recover': [recovered], 'status': [_terminal()]},
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        assert scheduler_authority.run_canonical_job(dict(job), execution_id=fire) == tuple(RESULT)
        params = {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}
        journal = json.loads(scheduler_authority.journal_path(job['id'], fire).read_text())
    assert _methods(seen[0]) == ['submit']
    assert seen[1] == [('recover', params), ('status', RECEIPT)]
    assert journal['receipt'] == RECEIPT


def test_lost_submit_the_owner_never_admitted_stays_unknown(tmp_path, monkeypatch):
    """``missing`` after a lost submit cannot prove the submit is not still in flight: fail closed
    and leave the journal to receipt recovery, never submit again."""
    seen = _gateway(monkeypatch, [
        {'submit': [GatewayClientError(DISCONNECTED)]},
        {'recover': [{'status': 'missing', 'result': None}]},
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler_authority.run_canonical_job(dict(job), execution_id=fire)
        assert scheduler_authority.journal_path(job['id'], fire).exists()
    assert [_methods(calls) for calls in seen] == [['submit'], ['recover']]


def test_gateway_gone_past_the_reconnect_budget_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_authority, 'RECONNECT_BUDGET_S', 1.0, raising=False)
    seen = _gateway(monkeypatch, [
        {'submit': [dict(RECEIPT)], 'status': [_started(), GatewayClientError(DISCONNECTED)]},
        GatewayUnavailableError('Gateway stopped: not_ready', 'stopped'),
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        with pytest.raises(scheduler_authority.CronExecutionUnknown, match='reconcile'):
            scheduler_authority.run_canonical_job(dict(job), execution_id=fire)
        journal = json.loads(scheduler_authority.journal_path(job['id'], fire).read_text())
    assert len(seen) > 2  # it did retry, with backoff, until the budget ran out
    assert all(calls == [] for calls in seen[1:])  # never re-submitted
    assert journal['receipt'] == RECEIPT  # left for receipt recovery


def test_a_fire_given_up_past_the_budget_is_finished_and_unpaused_by_its_receipt(tmp_path, monkeypatch):
    """The fail-closed pause is not the end: once the gateway is back and reports the receipt
    terminal, the next tick's receipt recovery books the run and lifts that pause by itself."""
    from cron import scheduler
    monkeypatch.setattr(scheduler_authority, 'RECONNECT_BUDGET_S', 0.2, raising=False)
    monkeypatch.setattr(scheduler_authority, 'RECONNECT_FIRST_DELAY_S', 0.05, raising=False)
    _gateway(monkeypatch, [
        {'submit': [dict(RECEIPT)], 'status': [_started(), GatewayClientError(DISCONNECTED)]},
        GatewayUnavailableError('Gateway stopped: not_ready', 'stopped'),
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        assert scheduler._run_one_job_body(dict(jobs.get_job(job['id']), execution_id=fire)) is False
        assert jobs.get_job(job['id'])['state'] == 'paused'
        journal = scheduler_authority.journal_path(job['id'], fire)
        assert journal.exists()

        seen = _gateway(monkeypatch, [{'recover': [dict(_terminal(), job=job, **RECEIPT)]}])
        scheduler_authority.reconcile_pending()
        saved = jobs.get_job(job['id'])
        assert (saved['state'], saved['enabled'], saved['paused_reason']) == ('scheduled', True, None)
        assert saved['last_status'] == 'ok'
        assert executions.get_execution(fire)['status'] == 'completed'
        assert not journal.exists()
    assert [_methods(calls) for calls in seen] == [['recover']]  # read back, never submitted again


def test_an_owner_refusal_is_an_answer_not_a_drop(tmp_path, monkeypatch):
    """A ``GatewayRPCError`` is the owner speaking: no reconnect, today's fail-closed path."""
    seen = _gateway(monkeypatch, [
        {'submit': [dict(RECEIPT)], 'status': [GatewayRPCError('permission_denied')]},
        {'status': [_terminal()]},
    ])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job, fire = _fire(tmp_path)
        with pytest.raises(scheduler_authority.CronExecutionUnknown):
            scheduler_authority.run_canonical_job(dict(job), execution_id=fire)
    assert len(seen) == 1
