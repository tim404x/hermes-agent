"""A firer that loses the gateway mid-observation must not send a failure notice for a run the owner
completed. 24 Nov 2026: the morning brief's worker gave up as ``unknown`` when a restart cut its
connection 1 s after the turn ended; receipt recovery booked the run ``completed`` at 13:16:42, yet the
"failed: Cron execution unverified" notice (parked while the gateway was down) reached the Crons chat
at 13:16:44 and the chat re-ran the brief. The notice now waits on the admission journal and goes out
only if recovery settles the fire ``unknown``."""
from contextlib import asynccontextmanager, contextmanager
import json

from cron import executions, jobs, scheduler, scheduler_authority
from hermes_cli import gateway_client


def _peer(monkeypatch, answer):
    class Peer:
        async def rpc(self, method, **params):
            return answer(method)

    @asynccontextmanager
    async def connected():
        yield Peer()
    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)


def _notices(monkeypatch, errors=()):
    sent = []
    errors = list(errors)

    def deliver(job, err_text, *, adapters, loop):
        sent.append((job['id'], err_text))
        return (errors.pop(0) if errors else None), 'delivered'
    monkeypatch.setattr(scheduler, '_deliver_crash_failure', deliver)
    return sent


def _unknown_fire(monkeypatch, job, fire):
    """The firer journals the fire, then loses its observer: CronExecutionUnknown pauses the job."""
    journal = scheduler_authority.journal_path(job['id'], fire)
    real_run_job = scheduler.run_job

    def run_job(job, **kwargs):
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': {'job_id': job['id'], 'request_id': fire,
                                                  'extra_prompt': None}, 'receipt': None}))
        raise scheduler_authority.CronExecutionUnknown('Cron execution unverified: Gateway disconnected')
    monkeypatch.setattr(scheduler, 'run_job', run_job)
    assert scheduler._run_one_job_body(dict(jobs.get_job(job['id']), execution_id=fire)) is False
    monkeypatch.setattr(scheduler, 'run_job', real_run_job)
    assert executions.get_execution(fire)['status'] == 'unknown'
    return journal


def _waiter_sees_unknown(monkeypatch, job, fire):
    """The gateway-side waiter of the restart-safe worker projects the unknown outcome."""
    from cron.scheduler_worker_failure import record_unknown_worker_outcome
    marks = []

    @contextmanager
    def owns(job_id, expected_owner):
        yield True
    monkeypatch.setattr(scheduler, 'fire_claim_fence', owns)
    monkeypatch.setattr(scheduler, 'mark_job_run', lambda *a, **k: marks.append(a) or True)
    assert record_unknown_worker_outcome(dict(job, execution_id=fire, fire_claim={'by': 'gateway'}))
    return marks


def test_the_waiter_parks_the_notice_while_recovery_can_still_prove_the_outcome(tmp_path, monkeypatch):
    sent = _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        marks = _waiter_sees_unknown(monkeypatch, job, fire)
        assert sent == []  # held, not sent
        assert len(marks) == 1  # the fire claim is still retired
        parked = json.loads(journal.with_suffix('.notice').read_text())
        assert 'Gateway disconnected' in parked['notice'] and parked['job']['id'] == job['id']


def test_the_waiter_still_notifies_at_once_when_nothing_will_settle_the_fire(tmp_path, monkeypatch):
    sent = _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        journal.unlink()  # never admitted: no receipt recovery will ever book it
        _waiter_sees_unknown(monkeypatch, job, fire)
        assert len(sent) == 1 and sent[0][0] == job['id']


def test_recovery_that_finds_the_run_completed_never_sends_the_parked_notice(tmp_path, monkeypatch):
    sent = _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        _waiter_sees_unknown(monkeypatch, job, fire)
        _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': job,
                                           'result': [True, 'document', 'answer', None]})
        scheduler_authority.reconcile_pending()
        assert executions.get_execution(fire)['status'] == 'completed'
        assert not journal.exists()
        assert sent == []
        scheduler_authority.reconcile_pending()
        assert sent == [] and not journal.with_suffix('.notice').exists()


def test_recovery_that_confirms_the_outcome_unknown_sends_the_parked_notice_once(tmp_path, monkeypatch):
    sent = _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        _waiter_sees_unknown(monkeypatch, job, fire)
        _peer(monkeypatch, lambda method: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        scheduler_authority.reconcile_pending()
        assert len(sent) == 1 and 'Gateway disconnected' in sent[0][1]
        assert journal.exists() and not journal.with_suffix('.notice').exists()


def test_a_parked_notice_survives_recovery_rewriting_the_journal(tmp_path, monkeypatch):
    """Recovery rewrites the journal (``pause_recorded``) from the copy it read before its await;
    the notice lives beside it, so that rewrite can't drop it."""
    sent = _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        record = json.loads(journal.read_text())
        record.pop('pause_recorded', None)
        journal.write_text(json.dumps(record))  # recovery will record its own pause

        def answer(method):
            _waiter_sees_unknown(monkeypatch, job, fire)  # parks while recovery awaits the owner
            return {'status': 'unknown', 'result': None}
        _peer(monkeypatch, answer)
        scheduler_authority.reconcile_pending()
        _peer(monkeypatch, lambda method: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        assert len(sent) == 1


def test_a_failing_notice_delivery_is_retried_then_dropped(tmp_path, monkeypatch):
    sent = _notices(monkeypatch, errors=['relay down', 'relay down', 'relay down'])
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        _waiter_sees_unknown(monkeypatch, job, fire)
        _peer(monkeypatch, lambda method: {'status': 'unknown', 'result': None})
        scheduler_authority.reconcile_pending()
        assert len(sent) == 1 and journal.with_suffix('.notice').exists()  # kept for a retry
        for _ in range(4):
            scheduler_authority.reconcile_pending()
        assert len(sent) == 3 and not journal.with_suffix('.notice').exists()


def test_without_the_built_in_ticker_the_waiter_notifies_at_once(tmp_path, monkeypatch):
    """A provider such as Chronos runs no receipt recovery: a parked notice would never go out."""
    sent = _notices(monkeypatch)
    monkeypatch.setattr(scheduler_authority, '_builtin_ticker', lambda: False)
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _unknown_fire(monkeypatch, job, fire)
        _waiter_sees_unknown(monkeypatch, job, fire)
        assert len(sent) == 1 and not journal.with_suffix('.notice').exists()
