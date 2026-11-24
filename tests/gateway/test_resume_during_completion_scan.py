"""A background completion batch must not hold a new viewer's session.resume handshake."""
import asyncio
from contextlib import contextmanager
import threading

import pytest

from tests.fixtures.storage_holder import raw_holder
from tests.gateway.test_session_end_delegations import _local
from tests.gateway.test_sibling_attach_while_profile_starting import _Socket
from tests.gateway.test_unviewed_completion_notes import _process_event, _target


@pytest.mark.asyncio
@pytest.mark.usefixtures('authority_cwd_in_tmp')  # a symbolic profile_id is a relative home
async def test_session_resume_answers_while_sqlite_completion_scan_is_busy(tmp_path, monkeypatch):
    from tui_gateway import server, ws

    runner, authority, ref, actor = await _local(tmp_path, monkeypatch)
    root = ref.session_id
    tip = _target(runner, authority, ref)
    events = [_process_event(authority, ref, tip, f'proc_{i}') for i in range(15)]
    runner._completion_notification_batch_window = 0

    async def burst():
        return await asyncio.gather(*(runner._enqueue_process_completion_notification(e['output'], e) for e in events))

    assert all(await burst())
    original = authority.persistence.admissions.list(session_id=root, pending_only=False)
    assert len(original) == 1 and len(original[0].payload['local_automation_v1']['identities']) == len(events)
    # Real JSON-RPC lanes and SQLite, no live socket, model, desktop watchers, or external delivery.
    monkeypatch.setattr(ws, '_start_backend_liveness', lambda: None)
    monkeypatch.setattr(server, '_ensure_skin_watcher', lambda: None)
    monkeypatch.setattr(server, '_ensure_lease_watcher', lambda: None)
    socket = _Socket(authority)
    loop = asyncio.get_running_loop()
    release, entered, waited_out = threading.Event(), threading.Event(), threading.Event()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'session.resume', 'params': {'session_id': root}}
    holder = raw_holder(authority.persistence)
    read = holder._read_ctx

    @contextmanager
    def held_read(*args, **kwargs):
        with read(*args, **kwargs) as conn:
            # Hold the actual identity SELECT inside sqlite3_step, not the RPC handler or all
            # readers. A busy WAL reader doesn't lock out a second viewer of the same store.
            def trace(sql):
                if 'instr(payload_json,' in sql and not entered.is_set():
                    entered.set()
                    loop.call_soon_threadsafe(socket.inbound.put_nowait, request)
                    if not release.wait(10):
                        waited_out.set()
            conn.set_trace_callback(trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(holder, '_read_ctx', held_read)
    serving = asyncio.create_task(ws.handle_ws(socket, auth_identity={
        'provider': 'local', 'user_id': actor.subject, 'profile_id': authority.profile_id,
        'instance_id': authority.instance_id, 'capabilities': sorted(actor.capabilities)}))
    # All 15 producer ACKs were lost. A new CLI connection must attach during the recheck.
    flushing = asyncio.create_task(burst())
    try:
        reply = await socket.reply(1)
        assert 'result' in reply, reply
        assert reply['result']['session_id'] == root and reply['result']['subscription_id']
        assert not waited_out.is_set() and not flushing.done(), 'completion scan held the owner loop until after resume'
    finally:
        release.set()
        socket.inbound.put_nowait(None)
        await asyncio.gather(serving, flushing)
    assert flushing.result() == [True] * len(events)
    assert authority.persistence.admissions.list(session_id=root, pending_only=False) == original
