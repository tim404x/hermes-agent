"""A freshly spawned managed worker waits out a stalled owner during ``worker.adopt``.

26 Nov 2026, 13:31 UTC: the gateway owner spawned a Kanban managed worker and then stalled
>10 s on its event loop. The worker's very first RPC (``worker.adopt``) hit
``discover_gateway_endpoint`` -> ``inaccessible/control_timeout`` while ``WorkerRPC.endpoint``
was still ``None``; ``WorkerRPC._connection()`` classified that as final ``owner_unavailable``
instead of retryable ``WorkerLinkLost('owner_unresponsive')``, and ``managed_worker.execute()``
called ``worker.adopt`` only once. The card died as a plain crash and hit its failure cap.

Pinned here:
- first connect to a stalled owner (``inaccessible/control_timeout``, ``endpoint is None``)
  raises ``WorkerLinkLost('owner_unresponsive')`` and never fabricates an endpoint;
- ``adopt_with_redial`` / ``execute`` waits out transient ``WorkerLinkLost`` failures with
  backoff and warning logs (without leaking scope secrets), then proceeds with the adopted
  result;
- owner refusals (``stale_epoch``, ``permission_denied``, ``stale_generation``) and plain
  ``WorkerPersistenceError`` (``owner_unavailable``, ``synchronous_self_rpc``) fail at once
  with zero retries;
- non-timeout absent/stopped/draining discovery on first connect stays final
  ``WorkerPersistenceError('owner_unavailable')``, not ``WorkerLinkLost``;
- budget exhaustion re-raises the last ``WorkerLinkLost`` (fail closed).
"""
import contextvars
import json
import logging
import os
import threading
from types import SimpleNamespace

import pytest


def _scope(home):
    return {
        'profile_id': str(home),
        'session_id': 'sess-adopt-1',
        'execution_id': 'admission-worker:abc123',
        'generation': 4,
        'pid': os.getpid(),
        'birth': 12345.0,
        'secret': 'top-secret-adoption-token-do-not-log',
        'epoch': 7,
    }


def test_a_first_connect_to_a_stalled_owner_is_link_lost_not_unavailable(tmp_path, monkeypatch):
    from hermes_cli import gateway_runtime
    from agent.runtime_session_store import WorkerLinkLost, WorkerRPC

    monkeypatch.setattr(
        gateway_runtime,
        'discover_gateway_endpoint',
        lambda home, timeout: SimpleNamespace(
            state='inaccessible', reason_code='control_timeout', endpoint=None
        ),
    )
    rpc = WorkerRPC(tmp_path)
    assert rpc.endpoint is None
    with pytest.raises(WorkerLinkLost, match='owner_unresponsive'):
        rpc('worker.adopt', session_id='sess-adopt-1')
    assert rpc.endpoint is None


@pytest.mark.parametrize(
    'state, reason_code',
    [
        ('stopped', None),
        ('absent', None),
        ('starting', None),
        ('draining', None),
        ('incompatible', 'runtime_protocol'),
        ('inaccessible', 'profile_mismatch'),
    ],
)
def test_no_owner_on_first_connect_stays_final(tmp_path, monkeypatch, state, reason_code):
    from hermes_cli import gateway_runtime
    from agent.runtime_session_store import WorkerLinkLost, WorkerPersistenceError, WorkerRPC

    monkeypatch.setattr(
        gateway_runtime,
        'discover_gateway_endpoint',
        lambda home, timeout: SimpleNamespace(state=state, reason_code=reason_code, endpoint=None),
    )
    rpc = WorkerRPC(tmp_path)
    assert rpc.endpoint is None
    with pytest.raises(WorkerPersistenceError, match='owner_unavailable') as exc_info:
        rpc('worker.adopt', session_id='sess-adopt-1')
    assert not isinstance(exc_info.value, WorkerLinkLost)
    assert rpc.endpoint is None


def test_worker_adopt_waits_out_a_stalled_owner_then_runs(tmp_path, monkeypatch, caplog):
    from agent import managed_worker, retry_utils
    from agent.runtime_session_store import WorkerLinkLost

    scope = _scope(tmp_path)
    calls = []
    sleeps = []
    expected = {'execution_id': scope['execution_id'], 'owner_epoch': scope['epoch'], 'status': 'running'}

    def fake_rpc(method, **params):
        calls.append((method, params))
        if len(calls) <= 2:
            raise WorkerLinkLost('owner_unresponsive')
        return expected

    monkeypatch.setattr(
        retry_utils,
        'jittered_backoff',
        lambda attempt, *, base_delay, max_delay, jitter_ratio: min(base_delay * (2 ** (attempt - 1)), max_delay),
    )
    monkeypatch.setattr(managed_worker.time, 'sleep', sleeps.append)
    caplog.set_level(logging.WARNING, logger='agent.managed_worker')

    adopted = managed_worker.adopt_with_redial(fake_rpc, scope)
    assert adopted == expected
    assert len(calls) == 3
    assert all(method == 'worker.adopt' for method, _ in calls)
    assert all('epoch' not in params for _, params in calls)
    assert sleeps == [0.5, 1.0]

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all('owner_unresponsive' in msg for msg in warnings)
    # Secrets and scope values must never appear in retry warnings.
    for forbidden in (scope['secret'], scope['execution_id'], scope['session_id']):
        assert all(forbidden not in msg for msg in warnings)


def test_execute_wires_worker_adopt_through_redial(tmp_path, monkeypatch):
    from agent import managed_worker
    from agent.runtime_session_store import WorkerLinkLost

    scope = _scope(tmp_path)
    frame = {'home': str(tmp_path), 'scope': scope}
    attempts = []
    handed = []
    expected = {'execution_id': scope['execution_id'], 'owner_epoch': scope['epoch'], 'status': 'running'}

    class _ReachedStore(Exception):
        pass

    class FakeRPC:
        def __init__(self, home):
            self.home = home

        def __call__(self, method, **params):
            attempts.append((method, params))
            if len(attempts) <= 2:
                raise WorkerLinkLost('owner_unresponsive')
            return expected

    def fake_store(rpc, store_scope, outbox, adopted):
        handed.append((store_scope, adopted))
        raise _ReachedStore()

    monkeypatch.setattr(managed_worker, 'bind_worker_policy', lambda f: None)
    monkeypatch.setattr(managed_worker.time, 'sleep', lambda _: None)
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', FakeRPC)
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', fake_store)

    def run_in_isolated_context():
        with pytest.raises(_ReachedStore):
            managed_worker.execute(frame, SimpleNamespace(send=lambda *a, **k: None))

    contextvars.copy_context().run(run_in_isolated_context)
    assert len(attempts) == 3
    assert [m for m, _ in attempts] == ['worker.adopt'] * 3
    assert handed == [(scope, expected)]


@pytest.mark.parametrize(
    'exc_factory',
    [
        lambda: __import__('agent.runtime_session_store', fromlist=['WorkerOwnerRefusal']).WorkerOwnerRefusal('stale_epoch'),
        lambda: __import__('agent.runtime_session_store', fromlist=['WorkerOwnerRefusal']).WorkerOwnerRefusal('permission_denied'),
        lambda: __import__('agent.runtime_session_store', fromlist=['WorkerOwnerRefusal']).WorkerOwnerRefusal('stale_generation'),
        lambda: __import__('agent.runtime_session_store', fromlist=['WorkerPersistenceError']).WorkerPersistenceError('owner_unavailable'),
        lambda: __import__('agent.runtime_session_store', fromlist=['WorkerPersistenceError']).WorkerPersistenceError('synchronous_self_rpc'),
    ],
)
def test_worker_adopt_refusal_is_final(tmp_path, monkeypatch, exc_factory):
    from agent import managed_worker

    scope = _scope(tmp_path)
    calls = []
    sleeps = []
    exc = exc_factory()

    def fake_rpc(method, **params):
        calls.append(method)
        raise exc

    monkeypatch.setattr(managed_worker.time, 'sleep', sleeps.append)
    with pytest.raises(type(exc), match=str(exc)):
        managed_worker.adopt_with_redial(fake_rpc, scope)
    assert calls == ['worker.adopt']
    assert sleeps == []


def test_worker_adopt_budget_exhaustion_reraises_last_link_lost(tmp_path, monkeypatch, caplog):
    from agent import managed_worker, retry_utils
    from agent.runtime_session_store import WorkerLinkLost

    scope = _scope(tmp_path)
    clock = [100.0]
    sleeps = []
    calls = []

    def fake_monotonic():
        return clock[0]

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    def fake_rpc(method, **params):
        calls.append(method)
        raise WorkerLinkLost(f'owner_unresponsive#{len(calls)}')

    monkeypatch.setattr(
        retry_utils,
        'jittered_backoff',
        lambda attempt, *, base_delay, max_delay, jitter_ratio: min(base_delay * (2 ** (attempt - 1)), max_delay),
    )
    monkeypatch.setattr(managed_worker.time, 'monotonic', fake_monotonic)
    monkeypatch.setattr(managed_worker.time, 'sleep', fake_sleep)
    caplog.set_level(logging.WARNING, logger='agent.managed_worker')

    with pytest.raises(WorkerLinkLost) as exc_info:
        managed_worker.adopt_with_redial(fake_rpc, scope, budget=5.0)

    # No attempt starts at the deadline: after 3.5 s the next 4 s backoff no longer fits in 5 s.
    assert len(calls) == 4
    assert str(exc_info.value) == 'owner_unresponsive#4'
    assert sleeps == [0.5, 1.0, 2.0]
    assert clock[0] - 100.0 < 5.0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == len(calls)  # one warning per retry + final give-up warning


def test_adopt_retry_warnings_never_carry_raw_transport_text(tmp_path, monkeypatch, caplog):
    """WorkerRPC wraps raw transport text (socket paths, profile ids) into WorkerLinkLost: the retry
    and give-up warnings log only known reason codes or exception type names."""
    from agent import managed_worker, retry_utils
    from agent.runtime_session_store import WorkerLinkLost

    # tests/agent/conftest.py zeroes jittered_backoff; a frozen clock would then never use up the budget.
    monkeypatch.setattr(retry_utils, 'jittered_backoff',
                        lambda attempt, *, base_delay, max_delay, jitter_ratio: min(base_delay * 2 ** (attempt - 1), max_delay))
    sentinel = 'sk-SECRET-SENTINEL /home/x/.hermes/profiles/p1/control.sock'
    clock = [0.0]
    monkeypatch.setattr(managed_worker.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(managed_worker.time, 'sleep', lambda d: clock.__setitem__(0, clock[0] + d))

    def fake_rpc(method, **params):
        try:
            raise OSError(sentinel)
        except OSError as cause:
            raise WorkerLinkLost(sentinel) from cause

    caplog.set_level(logging.WARNING, logger='agent.managed_worker')
    with pytest.raises(WorkerLinkLost):
        managed_worker.adopt_with_redial(fake_rpc, _scope(tmp_path), budget=3.0)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) >= 2 and any('giving up' in m for m in msgs)
    assert all('SENTINEL' not in m and 'control.sock' not in m for m in msgs)
    assert all('WorkerLinkLost from OSError' in m for m in msgs)


def test_real_worker_rpc_adopts_after_first_connect_control_timeout_clears(tmp_path, monkeypatch):
    """End-to-end over a real loopback WebSocket: discovery times out twice before the owner
    recovers; WorkerRPC never uses an undiscovered endpoint and adopt succeeds once ready."""
    from websockets.sync.server import serve
    from hermes_cli import gateway_client, gateway_runtime
    import hermes_cli.gateway_runtime_discovery as discovery_mod
    from agent import managed_worker
    from agent import runtime_session_store as store_module
    from agent.runtime_session_store import WorkerRPC

    monkeypatch.setattr(store_module, '_worker_process', store_module._worker_process)

    scope = _scope(tmp_path)
    received = []
    discover_calls = [0]

    def handler(ws):
        ws.close_timeout = 1
        for raw in ws:
            req = json.loads(raw)
            received.append(req['method'])
            ws.send(json.dumps({
                'jsonrpc': '2.0',
                'id': req['id'],
                'result': {'execution_id': scope['execution_id'], 'owner_epoch': scope['epoch'], 'status': 'running'},
            }))
        ws.close()

    with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = SimpleNamespace(
            api_origin=f'http://127.0.0.1:{server.socket.getsockname()[1]}',
            control_home=None,
        )

        def discover(home, timeout):
            discover_calls[0] += 1
            if discover_calls[0] <= 2:
                return SimpleNamespace(state='inaccessible', reason_code='control_timeout', endpoint=None)
            return SimpleNamespace(state='ready', reason_code=None, endpoint=endpoint)

        monkeypatch.setattr(gateway_runtime, 'discover_gateway_endpoint', discover)
        monkeypatch.setattr(discovery_mod, 'query_identify', lambda home, timeout: {'pid': -1})
        monkeypatch.setattr(gateway_client, '_session_ticket', lambda home, ep, purpose: 'ticket')
        monkeypatch.setattr(managed_worker.time, 'sleep', lambda _: None)
        try:
            rpc = WorkerRPC(tmp_path)
            adopted = managed_worker.adopt_with_redial(rpc, scope)
            assert adopted['owner_epoch'] == scope['epoch']
            assert adopted['status'] == 'running'
            assert discover_calls[0] == 3
            assert received == ['worker.adopt']
            assert rpc.endpoint is endpoint
            rpc.close()
        finally:
            server.shutdown()
