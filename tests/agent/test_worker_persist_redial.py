"""A managed worker's journaled ``worker.persist`` survives a lost owner link (t_7e4c9e3c).

24 Nov 2026, 1:14 PM PT: the owner's state.db writer mutex was held 35 s by a routing rewrite, the
worker's ``turn.acquire`` reply did not come within its receipt wait, ``WorkerRPC`` dropped the link
and raised ``worker_receipt_timeout``, the store latched it as a sticky failure, the turn died with
``managed_worker_failed`` and the Kanban attempt became "Kanban execution unknown" (work lost). The
owner had committed the write; its receipt replay (same sequence, same payload digest) would have
returned the stored result to a resend.

Pinned here against a real loopback WebSocket owner that commits each sequence once and replays it:
- a lost reply (owner commits, link drops) and a reply past the receipt wait: the same entry goes
  again on a fresh connection and the store carries on, nothing applied twice;
- an owner whose store is only busy (``storage_unavailable``): the same entry goes again;
- an owner that is gone past ``RECONNECT_BUDGET_S``: fails closed, the entry stays journaled;
- an owner refusal (``stale_epoch``): never sent again, never redialed.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from tests.waiting import wait_until

SCOPE = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}


@pytest.fixture
def owner(monkeypatch, tmp_path):
    from websockets.sync.server import serve
    from hermes_cli import gateway_client, gateway_runtime
    import hermes_cli.gateway_runtime_discovery as discovery
    import agent.runtime_session_store as store_module

    state = SimpleNamespace(connections=0, sends=[], committed={}, applied=[], plan=[], up=True,
                            release=threading.Event())

    def handler(ws):
        state.connections += 1
        ws.close_timeout = 1
        for raw in ws:
            request = json.loads(raw)
            params = request['params']
            state.sends.append(params.get('sequence'))
            action = state.plan.pop(0) if state.plan else 'answer'
            if action == 'refuse':
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                                    'error': {'code': 4001, 'message': 'stale_epoch'}}))
                continue
            if action == 'busy':
                ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                                    'error': {'code': 5001, 'message': 'storage_unavailable'}}))
                continue
            sequence = params['sequence']
            if sequence not in state.committed:  # the receipt transaction: commit once, replay after
                state.applied.append((sequence, params['operation']))
                state.committed[sequence] = {'value': f"{params['operation']}#{sequence}",
                                             'count': 1, 'annotations': [{}] * len(
                                                 params['payload'].get('messages', []))}
            if action == 'drop_after_commit':
                break  # the write committed; its reply never leaves
            if action == 'stall':
                state.release.wait(10)  # a stalled owner loop: the reply is late past the receipt wait
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': state.committed[sequence]}))
        ws.close()

    def ticket(home, endpoint, purpose):
        assert purpose == 'worker-adoption'
        return 'ticket'

    with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = SimpleNamespace(api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}',
                                   control_home=None)
        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', lambda home, timeout: SimpleNamespace(
            state='ready' if state.up else 'stopped', endpoint=endpoint if state.up else None))
        monkeypatch.setattr(discovery, 'query_identify', lambda home, timeout: {'pid': -1})
        monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
        monkeypatch.setattr(store_module, 'RECONNECT_BUDGET_S', 5.0, raising=False)
        monkeypatch.setattr(store_module, 'RECEIPT_WAIT_S', 0.5, raising=False)
        try:
            yield state
        finally:
            state.release.set()
            server.shutdown()


def _store(tmp_path):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    return RuntimeSessionStore(WorkerRPC(tmp_path), SCOPE, tmp_path / 'outbox')


def test_a_reply_lost_with_the_link_is_resent_on_a_fresh_connection_and_the_turn_goes_on(owner, tmp_path):
    store = _store(tmp_path)
    try:
        assert store.try_acquire_session_turn_lease('s', 'holder') == 'turn.acquire#1'
        owner.plan = ['drop_after_commit']
        # Committed by the owner, reply lost with the socket: the store sends sequence 2 again.
        assert store.refresh_session_turn_lease('s', 'holder') == 'turn.renew#2'
        assert owner.sends == [1, 2, 2]
        assert owner.applied == [(1, 'turn.acquire'), (2, 'turn.renew')]  # never applied twice
        assert owner.connections == 2
        assert store.failure is None and store.journal['pending'] == []
        # Nothing sticky is left: the next write goes through on the new connection.
        store.queue_token_counts('s', input_tokens=3)
        assert store.flush_token_counts()
        assert owner.sends == [1, 2, 2, 3]
    finally:
        store.close()


def test_a_reply_late_past_the_receipt_wait_is_resent_not_fatal(owner, tmp_path):
    store = _store(tmp_path)
    try:
        owner.plan = ['stall']
        threading.Timer(1.5, owner.release.set).start()
        assert store.try_acquire_session_turn_lease('s', 'holder') == 'turn.acquire#1'
        assert owner.sends[:2] == [1, 1]
        assert owner.applied == [(1, 'turn.acquire')]
        assert owner.connections >= 2
        assert store.failure is None and store.journal['pending'] == []
    finally:
        store.close()


def test_an_owner_whose_store_is_busy_gets_the_same_entry_again(owner, tmp_path):
    store = _store(tmp_path)
    try:
        owner.plan = ['busy', 'busy']
        assert store.try_acquire_session_turn_lease('s', 'holder') == 'turn.acquire#1'
        assert owner.sends == [1, 1, 1]
        assert owner.applied == [(1, 'turn.acquire')]
        assert owner.connections == 1  # an answer, not a lost link: no redial
    finally:
        store.close()


def test_an_owner_restart_window_is_waited_out(owner, tmp_path):
    store = _store(tmp_path)
    try:
        assert store.try_acquire_session_turn_lease('s', 'holder') == 'turn.acquire#1'
        owner.up = False
        owner.plan = ['drop_after_commit']
        threading.Timer(1.0, lambda: setattr(owner, 'up', True)).start()
        assert store.refresh_session_turn_lease('s', 'holder') == 'turn.renew#2'
        assert owner.applied == [(1, 'turn.acquire'), (2, 'turn.renew')]
        assert store.failure is None
    finally:
        store.close()


def test_an_owner_gone_past_the_budget_still_fails_closed_with_the_entry_journaled(owner, tmp_path, monkeypatch):
    import agent.runtime_session_store as store_module
    from agent.runtime_session_store import WorkerLinkLost, WorkerPersistenceError
    monkeypatch.setattr(store_module, 'RECONNECT_BUDGET_S', 1.0, raising=False)
    dials = []
    from hermes_cli import gateway_runtime
    real = gateway_runtime.discover_gateway_endpoint

    def counting(home, timeout):
        dials.append(time.monotonic())
        return real(home, timeout)
    monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', counting)
    store = _store(tmp_path)
    owner.up = False
    started = time.monotonic()
    with pytest.raises(WorkerLinkLost, match='owner_unavailable'):
        store.try_acquire_session_turn_lease('s', 'holder')
    assert 0.9 <= time.monotonic() - started < 5
    assert len(dials) > 2  # it did retry, with backoff, until the budget ran out
    assert store.failure == 'owner_unavailable'
    disk = json.loads(store.path.read_text())
    assert [entry['sequence'] for entry in disk['pending']] == [1]  # kept for adoption / replay
    with pytest.raises(WorkerPersistenceError, match='owner_unavailable'):
        store.close()


def test_an_owner_refusal_is_final_never_resent_or_redialed(owner, tmp_path):
    from agent.runtime_session_store import WorkerOwnerRefusal, WorkerPersistenceError
    store = _store(tmp_path)
    owner.plan = ['refuse']
    with pytest.raises(WorkerOwnerRefusal, match='stale_epoch'):
        store.try_acquire_session_turn_lease('s', 'holder')
    assert owner.sends == [1]
    assert owner.connections == 1
    assert store.failure == 'stale_epoch'
    with pytest.raises(WorkerPersistenceError, match='stale_epoch'):
        store.close()


def test_a_redial_is_never_an_rpc_level_resend(owner, tmp_path):
    """WorkerRPC alone still never resends a call it may have delivered: only the journal, which
    owns the replayable sequence, does."""
    from agent.runtime_session_store import WorkerLinkLost, WorkerRPC
    rpc = WorkerRPC(tmp_path)
    owner.plan = ['drop_after_commit']
    with pytest.raises(WorkerLinkLost):
        rpc('worker.persist', sequence=1, operation='turn.acquire', payload={})
    assert owner.sends == [1]
    wait_until(lambda: rpc.link is None)
