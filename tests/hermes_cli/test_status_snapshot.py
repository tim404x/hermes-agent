"""/api/status answers the Desktop's ~1 Hz poll from a stale-while-revalidate body.

One computed body costs ~10 worker-thread hops; under GIL contention that took seconds and the
Desktop's 2 s / 8 s status probes timed out while the backend was healthy. The serving process
(armed by ``_start_server_runtime``) returns the last body at once and refreshes it in the
background at most once per window; unarmed callers (tests, in-process awaits) compute every time.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_routers import status as status_router
from hermes_cli.web_routers import status_snapshot as snapshot_mod
from hermes_cli.web_routers.status_snapshot import StatusSnapshot
from tests.waiting import async_wait_until


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return _Clock()


def _counting_compute(bodies):
    calls = []

    async def compute():
        calls.append(1)
        await asyncio.sleep(0)
        return {"n": len(calls), **bodies.get(len(calls), {})}

    return compute, calls


@pytest.mark.asyncio
async def test_unarmed_snapshot_computes_every_call(clock):
    snap = StatusSnapshot(clock=clock)
    compute, calls = _counting_compute({})
    assert (await snap.get(compute))["n"] == 1
    assert (await snap.get(compute))["n"] == 2
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_fresh_body_is_served_without_computing(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    assert (await snap.get(compute))["n"] == 1
    clock.now += 2.9
    assert (await snap.get(compute))["n"] == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stale_body_is_returned_at_once_and_refreshed_once_in_the_background(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    await snap.get(compute)
    clock.now += 5
    # Three polls inside the stale window: all answered from the old body, ONE refresh started.
    answers = [(await snap.get(compute))["n"] for _ in range(3)]
    assert answers == [1, 1, 1]
    await async_wait_until(lambda: len(calls) == 2)
    await async_wait_until(lambda: snap._refresh.done())
    assert len(calls) == 2
    assert (await snap.get(compute))["n"] == 2


@pytest.mark.asyncio
async def test_concurrent_cold_callers_share_one_compute(clock):
    snap = StatusSnapshot(clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    bodies = await asyncio.gather(*(snap.get(compute) for _ in range(5)))
    assert [b["n"] for b in bodies] == [1] * 5
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_body_older_than_max_stale_is_recomputed_before_answering(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    await snap.get(compute)
    clock.now += 31
    assert (await snap.get(compute))["n"] == 2
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failed_background_refresh_keeps_the_last_body(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    state = {"fail": False}

    async def compute():
        if state["fail"]:
            raise RuntimeError("probe broke")
        return {"ok": True}

    await snap.get(compute)
    state["fail"] = True
    clock.now += 5
    assert await snap.get(compute) == {"ok": True}
    await async_wait_until(lambda: snap._refresh.done())
    assert await snap.get(compute) == {"ok": True}
    await async_wait_until(lambda: snap._refresh.done())
    clock.now += 40  # past max_stale, nothing in flight: the caller sees the failure, as an unarmed call would
    with pytest.raises(RuntimeError):
        await snap.get(compute)


@pytest.mark.asyncio
async def test_callers_get_copies_the_shared_body_never_changes(clock):
    snap = StatusSnapshot(clock=clock)
    snap.arm()

    async def compute():
        return {"gateway_platforms": {"telegram": {"state": "connected"}}}

    first = await snap.get(compute)
    first["gateway_platforms"]["telegram"]["state"] = "mutated"
    assert (await snap.get(compute))["gateway_platforms"]["telegram"]["state"] == "connected"


@pytest.mark.asyncio
async def test_a_wedged_refresh_times_out_and_the_next_caller_starts_a_fresh_one(clock):
    snap = StatusSnapshot(clock=clock, compute_timeout_s=0.05)
    snap.arm()
    never = asyncio.Event()

    async def wedged():
        await never.wait()
        return {"never": True}

    with pytest.raises(TimeoutError):
        await snap.get(wedged)
    compute, calls = _counting_compute({})
    assert (await snap.get(compute))["n"] == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_past_max_stale_a_refresh_already_running_never_holds_the_poll(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    hang = asyncio.Event()
    compute, calls = _counting_compute({})

    async def maybe_wedged():
        if len(calls) == 1:
            await hang.wait()
        return await compute()

    await snap.get(compute)
    clock.now += 5
    assert (await snap.get(maybe_wedged))["n"] == 1  # stale: served, and the refresh wedges
    clock.now += 60
    assert (await snap.get(maybe_wedged))["n"] == 1  # still the last body, not a hang
    hang.set()
    await async_wait_until(lambda: snap._refresh.done())
    assert (await snap.get(compute))["n"] == 2


_CALLER_HOME = contextvars.ContextVar("caller_home", default="launch")


@pytest.mark.asyncio
async def test_the_refresh_never_runs_in_the_triggering_callers_context(clock):
    """A body is computed for the launch profile, whichever request's scope happened to start it."""
    snap = StatusSnapshot(clock=clock)
    snap.arm()

    async def compute():
        return {"home": _CALLER_HOME.get()}

    token = _CALLER_HOME.set("secondary")
    try:
        assert (await snap.get(compute))["home"] == "launch"
    finally:
        _CALLER_HOME.reset(token)


@pytest.mark.asyncio
async def test_a_refresh_in_flight_at_disarm_never_stores_its_body(clock):
    snap = StatusSnapshot(clock=clock)
    snap.arm()
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return {"old": True}

    waiter = asyncio.ensure_future(snap.get(slow))
    await async_wait_until(lambda: snap._refresh is not None)
    in_flight = snap._refresh
    snap.disarm()
    snap.arm()
    release.set()
    assert (await waiter) == {"old": True}
    await async_wait_until(lambda: in_flight.done())
    assert snap._body is None


@pytest.fixture
def armed_route(monkeypatch):
    calls = []

    async def fake_compute(requested_profile, profile_dir):
        calls.append(requested_profile)
        return {"gateway_exit_reason": "telegram token rejected for bot 123 at /home/x", "profiles": ["default"],
                "gateway_platforms": {}, "gateway_state": "running", "multiplex_standalone_reason": "x"}

    monkeypatch.setattr(status_router, "_compute_status", fake_compute)
    # A frozen clock: TestClient runs each request on its own loop, and a body turning stale between two
    # requests on a slow box would start a refresh on a loop that is being torn down.
    monkeypatch.setattr(snapshot_mod.STATUS_SNAPSHOT, "_clock", _Clock())
    snapshot_mod.STATUS_SNAPSHOT.disarm()
    snapshot_mod.STATUS_SNAPSHOT.arm()
    yield calls
    snapshot_mod.STATUS_SNAPSHOT.disarm()


def test_route_serves_every_caller_class_from_one_compute(armed_route):
    from hermes_cli import web_server

    client = TestClient(web_server.app)
    token = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    verified = client.get("/api/status", headers=token).json()
    anonymous = client.get("/api/status").json()
    assert armed_route == [""]
    # The snapshot holds the full body; an unverified caller still gets the redacted copy.
    assert verified["gateway_exit_reason"].startswith("telegram token rejected")
    assert anonymous["gateway_exit_reason"] != verified["gateway_exit_reason"]
    assert client.get("/api/status", headers=token).json() == verified


def test_route_never_caches_a_profile_scoped_request(armed_route, monkeypatch):
    from hermes_cli import web_server
    from hermes_cli.web_routers import status as status_mod

    monkeypatch.setattr(status_mod, "_resolve_profile_dir", lambda name: None)

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(status_mod, "_config_profile_scope", lambda name: _Scope())
    client = TestClient(web_server.app)
    token = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    client.get("/api/status?profile=coder", headers=token)
    client.get("/api/status?profile=coder", headers=token)
    assert armed_route == ["coder", "coder"]


def test_route_never_caches_a_request_scoped_to_another_home(armed_route, monkeypatch):
    """A native profile ticket (or any home override) scopes the request to that profile's home: its body is
    computed for it, and never stored as the launch snapshot."""
    import hermes_constants
    from hermes_cli import web_server

    monkeypatch.setattr(hermes_constants, "get_hermes_home_override", lambda: "/tmp/another-profile-home")
    client = TestClient(web_server.app)
    token = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    client.get("/api/status", headers=token)
    client.get("/api/status", headers=token)
    assert armed_route == ["", ""]
    assert snapshot_mod.STATUS_SNAPSHOT._body is None
