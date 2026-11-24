"""Receipt recovery of a dead firer's fire never takes over a newer fire of the same job.

GPT 6.2 review of t_de6b9ac2 (24 Nov 2026): fire A was journaled and its firer died without marking
it (no provisional entry), then the tick claimed fire B. Recovery booked A through ``mark_job_run``'s
main path, which cleared B's fire claim, so B's own completion was refused as a stale owner ("fire
claim owner changed; discarding stale completion"). A's journal now records A's claim: a stored
claim held by anyone else is a newer fire's and stays, with its markers, slot and schedule; when a
newer fire is already booked (by claim acquisition order, ``seq``), A's outcome is historical.
"""
from datetime import timedelta
import json

import pytest

from cron import executions, jobs, scheduler_authority
from cron.extra_fire import _CLAIM_KEY
from tests.cron.test_parked_unknown_notice import _notices, _peer


def _dead_firer_fire(job, *, claim=None, stamp=True):
    """Fire A: journaled (with the claim its firer held), then given up without any mark."""
    fire = executions.create_execution(job['id'], source='test')['id']
    journal = scheduler_authority.journal_path(job['id'], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    record = {'params': {'job_id': job['id'], 'request_id': fire, 'extra_prompt': None}, 'receipt': None}
    if stamp:
        record['fire_claim'] = claim if claim is not None else {'by': 'tick-A', 'seq': 1}
    journal.write_text(json.dumps(record))
    assert executions.give_up_execution(fire, reason='firer died')
    return fire, journal


def _claim_b(job_id, **extra):
    claim = {'by': 'tick-B', 'seq': 2, 'at': jobs._hermes_now().isoformat(), **extra}
    jobs.update_job(job_id, {'fire_claim': claim})
    return claim


def _receipt(monkeypatch, job_id, result, frozen=None, **flags):
    """The owner answers with the job it froze at admission (default: today's record, which
    holds B's claim, so recovery must not take A's identity from it)."""
    _peer(monkeypatch, lambda method: {'status': 'terminal', 'job': frozen or jobs.get_job(job_id),
                                       'result': result, 'job_flags': flags})


@pytest.fixture
def store(tmp_path, monkeypatch):
    _notices(monkeypatch)
    with jobs.use_cron_store(tmp_path / 'cron'):
        yield


def test_recovery_keeps_the_newer_fires_claim_and_its_completion_lands(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    _fire, journal = _dead_firer_fire(job)
    claim = _claim_b(job['id'])
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not journal.exists()
    assert after['fire_claim'] == claim and after['last_status'] == 'ok'
    assert after['repeat']['completed'] == 1  # A counted once
    assert jobs.mark_job_run(job['id'], False, error='boom', expected_fire_owner='tick-B')
    final = jobs.get_job(job['id'])
    assert final['fire_claim'] is None and final['last_status'] == 'error'
    assert final['repeat']['completed'] == 2 and final['failure_streak'] == 1


def test_a_newer_manual_runs_markers_and_kept_slot_survive(store, monkeypatch):
    """B is a run-now that kept the :00 slot in next_run_at: A's recovery must not recompute it,
    and B's completion still settles as a manual run (no repeat slot spent)."""
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    _fire, _journal = _dead_firer_fire(job)
    slot = (jobs._hermes_now() + timedelta(minutes=10)).isoformat()
    jobs.update_job(job['id'], {'next_run_at': slot, 'manual_run_prompt': 'extra context'})
    _claim_b(job['id'], **{_CLAIM_KEY: slot})
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['next_run_at'] == slot and after['manual_run_prompt'] == 'extra context'
    assert after['repeat']['completed'] == 1
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner='tick-B')
    final = jobs.get_job(job['id'])
    assert final['next_run_at'] == slot and final['repeat']['completed'] == 1


def test_an_unreachable_recovery_plans_no_ladder_under_a_running_newer_fire(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1d', deliver='local')
    _fire, _journal = _dead_firer_fire(job)
    due = jobs.get_job(job['id'])['next_run_at']
    _claim_b(job['id'])
    _receipt(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'],
             _model_unreachable=True, _quota_hold_seconds=3600)
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['last_status'] == 'error' and after['failure_streak'] == 1
    assert 'unreachable_retry' not in after and not after.get('quota_hold_until')
    assert after['next_run_at'] == due


def test_a_recovery_after_the_newer_run_booked_is_historical(store, monkeypatch):
    """B's ledger row was allocated first (the ticker allocates before dispatch) but A acquired the
    claim first: acquisition order decides, so A's success never replaces B's booked failure."""
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    fire_b = executions.create_execution(job['id'], source='test')['id']
    fire_a, journal = _dead_firer_fire(job)
    _claim_b(job['id'])
    assert jobs.mark_job_run(job['id'], False, error='boom', expected_fire_owner='tick-B',
                             execution_id=fire_b)
    booked = jobs.get_job(job['id'])
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert not journal.exists()
    for field in ('last_status', 'last_error', 'last_run_at', 'failure_streak', 'next_run_at',
                  'last_failure'):
        assert after.get(field) == booked.get(field), field
    assert after['repeat']['completed'] == 2  # both fires counted
    assert after['canonical_completions'][-1] == fire_b and fire_a in after['canonical_completions']
    assert executions.get_execution(fire_a)['status'] == 'completed'  # the ledger keeps A's result
    scheduler_authority.reconcile_pending()  # nothing left to replay
    assert jobs.get_job(job['id'])['repeat']['completed'] == 2


def test_a_newer_cancelled_booking_without_an_execution_id_still_orders(store, monkeypatch):
    """Manual A dies, B is cancelled (booked with no execution id): A is history, not counted."""
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local', repeat=2)
    _fire, _journal = _dead_firer_fire(job, claim={'by': 'tick-A', 'seq': 1, _CLAIM_KEY: 'slot'})
    _claim_b(job['id'])
    assert jobs.mark_job_run(job['id'], False, status='cancelled', expected_fire_owner='tick-B')
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['last_status'] == 'cancelled' and after['state'] == 'scheduled'
    assert after['repeat']['completed'] == 1  # B counted; manual A spends no repeat run


def test_a_recovery_that_reaches_the_limit_retires_under_a_running_manual_fire(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local', repeat=1)
    _fire, _journal = _dead_firer_fire(job)
    slot = (jobs._hermes_now() + timedelta(minutes=10)).isoformat()
    jobs.update_job(job['id'], {'next_run_at': slot})
    _claim_b(job['id'], **{_CLAIM_KEY: slot})
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    assert jobs.get_job(job['id'])['state'] == 'completed'
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner='tick-B')
    final = jobs.get_job(job['id'])
    assert final['state'] == 'completed' and final['enabled'] is False
    assert final['next_run_at'] is None and final['repeat']['completed'] == 1
    assert jobs.claim_job_for_fire(job['id']) is False


def test_a_replaced_provisional_outcome_leaves_a_running_manual_fires_slot(store, monkeypatch):
    from tests.cron.test_parked_unknown_notice import _unknown_fire, _waiter_marks_unknown
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    fire = executions.create_execution(job['id'], source='test')['id']
    _unknown_fire(monkeypatch, job, fire)
    _waiter_marks_unknown(monkeypatch, job, fire)  # A's provisional mark retired its claim
    slot = (jobs._hermes_now() + timedelta(minutes=40)).isoformat()
    jobs.update_job(job['id'], {'next_run_at': slot})
    _claim_b(job['id'], **{_CLAIM_KEY: slot})
    _receipt(monkeypatch, job['id'], [False, 'document', '', 'DNS lookup failed'], _model_unreachable=True)
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['next_run_at'] == slot and 'unreachable_retry' not in after
    assert after['fire_claim']['by'] == 'tick-B'


def test_a_queued_run_now_survives_an_older_fires_recovery(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    _fire, _journal = _dead_firer_fire(job)
    queued = jobs.trigger_job(job['id'], extra_prompt='look at X')
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    after = jobs.get_job(job['id'])
    assert after['manual_run_prompt'] == 'look at X' and after['next_run_at'] == queued['next_run_at']
    assert after['manual_run_at'] == queued['manual_run_at'] and after['repeat']['completed'] == 1


def test_a_recovered_retry_rung_is_not_counted_as_a_new_occurrence(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local', repeat=3)
    rung = (jobs._hermes_now() - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    jobs.update_job(job['id'], {'unreachable_retry': {'attempt': 1, 'at': rung},
                                'repeat': {'times': 3, 'completed': 1}})
    _fire, journal = _dead_firer_fire(job)
    record = json.loads(journal.read_text())
    from cron.occurrences import scheduled_instant
    journal.write_text(json.dumps(dict(record, scheduled_instant=scheduled_instant(rung))))
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    assert not journal.exists() and jobs.get_job(job['id'])['last_status'] == 'ok'
    assert jobs.get_job(job['id'])['repeat']['completed'] == 1


def test_a_dead_fires_own_lingering_claim_is_still_cleared(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    _fire, _journal = _dead_firer_fire(job)
    jobs.update_job(job['id'], {'fire_claim': {'by': 'tick-A', 'seq': 1, 'at': jobs._hermes_now().isoformat()}})
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None])
    scheduler_authority.reconcile_pending()
    assert jobs.get_job(job['id'])['fire_claim'] is None


def test_a_journal_without_the_stamp_reads_the_job_frozen_at_admission(store, monkeypatch):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    _fire, _journal = _dead_firer_fire(job, stamp=False)
    frozen = dict(jobs.get_job(job['id']),
                  fire_claim={'by': 'tick-A', 'seq': 1, 'at': jobs._hermes_now().isoformat()})
    claim = _claim_b(job['id'])
    _receipt(monkeypatch, job['id'], [True, 'document', 'answer', None], frozen=frozen)
    scheduler_authority.reconcile_pending()
    assert jobs.get_job(job['id'])['fire_claim'] == claim


def test_the_firer_stamps_its_claim_on_the_admission_journal(store, monkeypatch):
    from gateway import session_cron
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    claimed = dict(jobs.get_job(job['id']), _scheduled_instant='2026-11-24T15:00:00+00:00',
                   fire_claim={'by': 'tick-A', 'seq': 4, 'at': 'x', _CLAIM_KEY: 'slot'})
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)

    def answer(method):
        raise ConnectionError('gateway went away')
    _peer(monkeypatch, answer)
    with pytest.raises(scheduler_authority.CronExecutionUnknown):
        scheduler_authority.run_canonical_job(claimed, execution_id='fire-a')
    record = json.loads(scheduler_authority.journal_path(job['id'], 'fire-a').read_text())
    assert record['fire_claim'] == {'by': 'tick-A', 'seq': 4, _CLAIM_KEY: 'slot'}  # not the lease
    assert record['scheduled_instant'] == '2026-11-24T15:00:00+00:00'


def test_claims_carry_their_acquisition_order(store):
    job = jobs.create_job(prompt='brief', schedule='every 1h', deliver='local')
    first = jobs.claim_job_for_fire(job['id'], return_job=True)
    assert first['fire_claim']['seq'] == 1
    assert jobs.mark_job_run(job['id'], True, expected_fire_owner=first['fire_claim']['by'])
    assert jobs.get_job(job['id'])['booked_fire_seq'] == 1
    second = jobs.claim_job_for_fire(job['id'], force=True, return_job=True)
    assert second['fire_claim']['seq'] == 2
