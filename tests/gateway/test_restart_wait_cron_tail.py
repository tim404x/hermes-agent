"""A deferred restart waits for a cron turn's firer to read its receipt (bounded).

24 Nov 2026 (morning brief): the brief's turn ended inside the gateway at 13:14:51.334 and the
restart-when-idle wait saw idle 0.6 s later. The executing session no longer held it and the
scheduler's in-flight key of a scoped (restart-safe) worker never does, so the gateway stopped before
the worker's next ``cron.status`` poll. The worker lost its connection, gave up as ``unknown``, paused
the job and a false failure notice reached the Crons chat, which re-ran the brief.
"""
import asyncio
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import storage_handle
from tests.gateway.restart_test_helpers import make_restart_runner


def _finished_cron_turn(tmp_path, monkeypatch):
    from cron import scheduler
    from gateway import run, session_cron
    from gateway.session_authority import LiveSession, SessionAuthority, admission_row
    from gateway.session_contract import SessionActor, SessionRef

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    db = storage_handle(tmp_path)
    seed_session(db, 's', 'cron')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='p', instance_id='test',
                                 epoch=db.runtime.begin_epoch(instance_id='test'), persistence=storage_handle(tmp_path))
    authority.sessions['s'] = LiveSession(SimpleNamespace(platform=None, user_id='cron-owner'), 'route')
    monkeypatch.setattr(run, '_profile_runtime_scope', lambda home: nullcontext())
    monkeypatch.setattr(scheduler, 'run_job',
                        lambda job, *, extra_prompt, execution_id, cancel_event: (True, 'doc', 'answer', None))
    actor = SessionActor('cron-owner', 'p', frozenset({'session:create', 'session:submit', 'session:control'}), 'ticker')
    db.admissions.admit(epoch=authority.epoch, principal_id='cron-owner', session_id='s',
                        request_id='cron:brief:fire', payload={'text': ''})
    row = admission_row(db.admissions.claim(epoch=authority.epoch, session_id='s'))
    policy = SimpleNamespace(request_json=json.dumps({
        'extra_prompt': None, 'request_id': 'cron:brief:fire', 'cron_job': {'id': 'brief', 'name': 'Brief'}}))
    runner, _adapter = make_restart_runner()
    runner.session_authority = authority
    runner._scale_to_zero_status = lambda *a, **k: None

    async def execute():
        return await session_cron.execute(authority, SessionRef('p', 's'), row, policy)

    async def poll():
        return await session_cron.operation(
            authority, 'status', {'session_id': 's', 'admission_id': row['admission_id']}, actor)

    def settle():
        db.admissions.settle(epoch=authority.epoch, admission_id=row['admission_id'], generation=row['generation'],
                             outcome='completed',
                             result={'result': {'cron_result': [True, 'doc', 'answer', None]}, 'usage': {}})
    return db, authority, runner, execute, poll, settle


@pytest.mark.asyncio
async def test_a_finished_cron_turn_holds_the_restart_until_its_firer_reads_the_receipt(tmp_path, monkeypatch):
    from gateway import session_cron
    from gateway.restart_when_idle import await_idle
    monkeypatch.setattr(session_cron, 'CRON_TAIL_ACK_LINGER_S', 0.3)
    db, _authority, runner, execute, poll, settle = _finished_cron_turn(tmp_path, monkeypatch)
    try:
        assert runner._awaitable_work_count() == 0
        assert await execute() == 'answer'
        # The turn is over; its worker has not read the receipt yet: the restart must wait.
        assert runner._awaitable_work_count() == 1
        assert [u['job_id'] for u in runner._describe_active_work() if u['kind'] == 'cron_tail'] == ['brief']
        waiter = asyncio.create_task(await_idle(runner))
        await asyncio.sleep(0.5)
        assert not waiter.done(), 'the restart went ahead while the firer had not read its receipt'

        assert (await poll())['status'] == 'started'  # not settled yet: still held
        assert runner._awaitable_work_count() == 1
        settle()
        state = await poll()
        assert state['status'] == 'terminal' and state['result'] == [True, 'doc', 'answer', None]
        # The answer still has to leave on the firer's socket: held for the short linger only.
        assert runner._awaitable_work_count() == 1 and not waiter.done()
        assert await asyncio.wait_for(waiter, 5) is True
        assert runner._awaitable_work_count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_a_firer_that_never_reads_its_receipt_stops_holding_the_restart(tmp_path, monkeypatch):
    """Bounded: a worker that died after its turn ended holds a restart for the grace only."""
    from gateway import session_cron
    db, authority, runner, execute, _poll, _settle = _finished_cron_turn(tmp_path, monkeypatch)
    try:
        await execute()
        assert len(session_cron.unobserved_cron_units(runner)) == 1
        (admission_id, (deadline, job_id, sid)), = authority._cron_unobserved.items()
        assert deadline - time.monotonic() <= session_cron.CRON_TAIL_GRACE_S
        authority._cron_unobserved[admission_id] = (time.monotonic() - 1, job_id, sid)  # grace spent
        assert session_cron.unobserved_cron_units(runner) == []
        assert runner._awaitable_work_count() == 0
        assert authority._cron_unobserved == {}
    finally:
        db.close()
