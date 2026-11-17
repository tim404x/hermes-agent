"""Inline WS RPCs keep answering while the event loop's default executor is saturated.

Inline RPCs (``session.create``, ``prompt.submit``, ``session.interrupt``, ``ping``) are the client's
controls. They used to be dispatched with ``asyncio.to_thread``, the same default executor every REST
handler's blocking work runs on. A burst of slow transcript reads (``GET /api/sessions/<id>/messages``)
held all of its threads, and a 15 ms ``session.create`` answered after the Desktop's 30 s timeout: the
new tab never opened. Measured on a live backend: 14 concurrent transcript pages took a ``ping`` from
3 ms to 7.5 s.

The real ``handle_ws`` and ``_dispatch_request``; only ``server.dispatch`` is a stand-in.
"""
import asyncio
import concurrent.futures
import contextvars
import json
import threading

import pytest

from tests.waiting import async_wait_until
from tui_gateway import server
from tui_gateway import ws as ws_mod


class Socket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.sent = []

    async def accept(self):
        pass

    async def send_text(self, line):
        self.sent.append(json.loads(line))

    async def receive_text(self):
        item = await self.inbound.get()
        if item is None:
            raise ws_mod._WebSocketDisconnect()
        return json.dumps(item)

    async def close(self):
        pass


@pytest.fixture
def rig(monkeypatch):
    threads = []

    def dispatch(req, transport=None):
        threads.append(threading.current_thread().name)
        return {"jsonrpc": "2.0", "id": req["id"], "result": {"pong": True}}

    monkeypatch.setattr(server, "dispatch", dispatch)
    monkeypatch.setattr(server, "resolve_skin", dict)
    monkeypatch.setattr(server, "register_live_transport", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_start_backend_heartbeat_refresher", lambda: None)
    monkeypatch.setattr(server, "_schedule_startup_orphan_sweep", lambda: None, raising=False)
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "_ensure_lease_watcher", lambda: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(server, "_close_sessions_for_transport", lambda transport, end_reason: (0, 0))
    monkeypatch.setattr(ws_mod, "_start_backend_liveness", lambda: None)
    return Socket(), threads


@pytest.mark.asyncio
async def test_inline_rpc_answers_while_the_default_executor_is_saturated(rig):
    sock, threads = rig
    workers = 2
    shared = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="shared-default")
    asyncio.get_running_loop().set_default_executor(shared)
    release, holding = threading.Event(), []

    def slow_rest_read():
        holding.append(threading.current_thread().name)
        release.wait(30)

    task = asyncio.create_task(ws_mod.handle_ws(sock))
    blockers = []
    try:
        # The connection is up (its ready frame went out) before the REST surface goes busy.
        await async_wait_until(lambda: bool(sock.sent), what="gateway.ready")
        # Every default-executor thread is held and more work is queued behind them.
        blockers = [asyncio.ensure_future(asyncio.to_thread(slow_rest_read)) for _ in range(workers * 3)]
        await async_wait_until(lambda: len(holding) == workers, what="a saturated default executor")
        sock.inbound.put_nowait({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
        await async_wait_until(lambda: any(frame.get("id") == 7 for frame in sock.sent), timeout=5.0,
                               what="the inline RPC's reply while the default executor is still saturated")
        assert not release.is_set() and len(holding) == workers
    finally:
        release.set()
        await asyncio.gather(*blockers, return_exceptions=True)
        sock.inbound.put_nowait(None)
        await asyncio.wait_for(task, timeout=10)
        shared.shutdown(wait=False)
    assert threads and all(name.startswith("ws-rpc-dispatch") for name in threads), threads


@pytest.mark.asyncio
async def test_rpc_dispatch_thread_sees_the_callers_context():
    """``asyncio.to_thread`` copied the caller's contextvars; the dedicated pool does too."""
    marker = contextvars.ContextVar("marker", default="unset")
    marker.set("caller")
    assert await ws_mod._dispatch_in_rpc_thread(marker.get) == "caller"
    assert await ws_mod._dispatch_in_rpc_thread(lambda a, b: a + b, 2, 3) == 5
