"""/api/status answers the Desktop's ~1 Hz poll from a stale-while-revalidate body.

One computed body costs ~10 worker-thread hops; under GIL contention that took seconds and the
Desktop's 2 s / 8 s status probes timed out while the backend was healthy. The serving process
(armed by ``_start_server_runtime``) returns the last body at once and refreshes it in the
background at most once per window; unarmed callers (tests, in-process awaits) compute every time.
"""

from __future__ import annotations

import asyncio
import contextlib
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
    clock.now += 29  # 34 s old: past max_stale, refresh still running -> still the last body, not a hang
    assert (await snap.get(maybe_wedged))["n"] == 1
    hang.set()
    await async_wait_until(lambda: snap._refresh.done())
    assert (await snap.get(compute))["n"] == 2


@pytest.mark.asyncio
async def test_a_probe_that_keeps_wedging_never_freezes_a_healthy_body(clock):
    """Served stale only up to max_stale_s + compute_timeout_s: older than that, the poll waits for the
    refresh (and gets its failure) instead of reporting a minutes-old healthy body."""
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, compute_timeout_s=20.0, clock=clock)
    snap.arm()
    hang = asyncio.Event()

    async def wedged():
        await hang.wait()
        return {"late": True}

    async def healthy():
        return {"ok": True}

    await snap.get(healthy)
    clock.now += 5
    assert await snap.get(wedged) == {"ok": True}  # starts the refresh that wedges
    clock.now += 46  # 51 s old, past 30 + 20
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(snap.get(wedged), 0.2)  # the caller now waits on the refresh, not served
    hang.set()
    await async_wait_until(lambda: snap._refresh.done())


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


# ── invalidate: a write this server made shows on the next poll (red-team item 7) ──────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_retires_a_fresh_body(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    assert (await snap.get(compute))["n"] == 1
    snap.invalidate()
    assert (await snap.get(compute))["n"] == 2
    assert (await snap.get(compute))["n"] == 2  # and the new body is fresh again
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_refresh_started_before_invalidate_never_answers_a_later_caller(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock)
    snap.arm()
    compute, _calls = _counting_compute({})
    await snap.get(compute)
    clock.now += 5  # stale: the next poll answers at once and starts a refresh that reads the pre-write state
    release = asyncio.Event()

    async def pre_write():
        await release.wait()
        return {"n": "pre-write"}

    assert (await snap.get(pre_write))["n"] == 1
    old_refresh = snap._refresh
    snap.invalidate()
    assert (await snap.get(compute))["n"] == 2  # a compute that started after the write
    release.set()
    await async_wait_until(lambda: old_refresh.done())
    assert (await snap.get(compute))["n"] == 2  # the pre-write refresh never stored its body


@pytest.mark.asyncio
async def test_after_invalidate_the_wait_is_bounded_and_the_last_body_answers(clock):
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, clock=clock, invalidate_wait_s=0.05)
    snap.arm()
    compute, _calls = _counting_compute({})
    await snap.get(compute)
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return {"n": "post-write"}

    snap.invalidate()
    assert (await snap.get(slow))["n"] == 1  # held at most invalidate_wait_s, then the last body
    release.set()
    await async_wait_until(lambda: snap._refresh.done())
    assert (await snap.get(compute))["n"] == "post-write"


@pytest.mark.asyncio
async def test_after_invalidate_a_body_past_the_wedge_bound_never_answers(clock):
    """The bounded read-your-writes fallback keeps the wedge guarantee: a body older than max_stale_s +
    compute_timeout_s is not served after a write either; the poll waits on the refresh."""
    snap = StatusSnapshot(fresh_s=3.0, max_stale_s=30.0, compute_timeout_s=20.0, clock=clock, invalidate_wait_s=0.05)
    snap.arm()
    hang = asyncio.Event()

    async def wedged():
        await hang.wait()
        return {"late": True}

    async def healthy():
        return {"ok": True}

    await snap.get(healthy)
    clock.now += 51  # past 30 + 20
    snap.invalidate()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(snap.get(wedged), 0.2)
    hang.set()
    await async_wait_until(lambda: snap._refresh.done())


@pytest.mark.asyncio
async def test_a_failed_compute_after_invalidate_answers_with_the_last_body(clock):
    snap = StatusSnapshot(clock=clock)
    snap.arm()
    compute, _calls = _counting_compute({})
    await snap.get(compute)

    async def broken():
        raise RuntimeError("probe failed")

    snap.invalidate()
    assert (await snap.get(broken))["n"] == 1


@pytest.mark.asyncio
async def test_invalidate_is_safe_from_another_thread(clock):
    import threading

    snap = StatusSnapshot(clock=clock)
    snap.arm()
    compute, calls = _counting_compute({})
    await snap.get(compute)
    workers = [threading.Thread(target=snap.invalidate) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert (await snap.get(compute))["n"] == 2
    assert len(calls) == 2


@pytest.mark.parametrize("method, path, retires", [
    ("PUT", "/api/config", True), ("PUT", "/api/config/raw", True), ("PUT", "/api/env", True),
    ("DELETE", "/api/env", True), ("POST", "/api/gateway/restart", True), ("POST", "/api/gateway/stop", True),
    ("PUT", "/api/messaging/platforms/telegram", True), ("POST", "/api/profiles", True),
    ("DELETE", "/api/profiles/coder", True), ("POST", "/api/providers/oauth/nous/submit", True),
    ("POST", "/api/webapp-access/start", True), ("POST", "/api/hermes/update", True),
    ("GET", "/api/config", False), ("GET", "/api/status", False), ("POST", "/api/gateway-connections/last-used", False),
    ("POST", "/api/files/upload", False), ("POST", "/api/chat/image-upload", False), ("POST", "/api/audio/speak", False),
])
def test_only_status_relevant_writes_retire_the_snapshot(method, path, retires):
    assert snapshot_mod.is_status_write(method, path) is retires


def test_a_settings_write_retires_the_snapshot_and_the_next_poll_reads_it(armed_route):
    """The Desktop's settings page writes over REST; its next /api/status poll (about a second later) must show
    the write, not the snapshot taken before it."""
    from hermes_cli import web_server

    client = TestClient(web_server.app)
    token = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    client.get("/api/status", headers=token)
    client.get("/api/status", headers=token)
    assert armed_route == [""]
    # An unrelated write and a read leave the snapshot alone.
    client.get("/api/config", headers=token)
    client.post("/api/files/mkdir", headers=token, content=b"{")
    client.get("/api/status", headers=token)
    assert armed_route == [""]
    assert client.put("/api/config", headers=token, json={"config": {}}).status_code == 200
    client.get("/api/status", headers=token)
    assert armed_route == ["", ""]


def test_a_refused_write_never_reaches_the_snapshot(armed_route):
    """A write refused before it wrote (auth, validation, a public path's unknown method: any 4xx) retires nothing,
    so no caller, signed in or not, can keep every poll recomputing."""
    from hermes_cli import web_server

    client = TestClient(web_server.app)
    token = {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}
    client.get("/api/status", headers=token)
    assert client.put("/api/config", content=b"{}").status_code in (401, 403)
    assert client.put("/api/config", headers={**token, "content-type": "application/json"}, content=b"{").status_code == 422
    assert client.post("/api/status").status_code == 405
    client.get("/api/status", headers=token)
    assert armed_route == [""]


@pytest.mark.parametrize("outcome, retires", [("ok", 1), ("server-error", 1), ("raises", 1), ("refused", 0)])
def test_the_middleware_retires_once_per_write_that_may_have_written(monkeypatch, outcome, retires):
    retired = []
    monkeypatch.setattr(snapshot_mod, "invalidate_status", lambda: retired.append(1))

    async def app(scope, receive, send):
        if outcome == "raises":
            raise RuntimeError("handler failed before answering")
        status = {"ok": 200, "server-error": 500, "refused": 422}[outcome]
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(_message):
        return None

    middleware = snapshot_mod.StatusWriteInvalidation(app)
    scope = {"type": "http", "method": "PUT", "path": "/api/config"}
    with contextlib.suppress(RuntimeError):
        asyncio.run(middleware(scope, None, send))
    assert len(retired) == retires


def test_invalidate_status_retires_the_topology_ttl_cache(monkeypatch):
    from hermes_cli import web_server_gateway as gw

    collects = []

    def collect():
        collects.append(1)
        return {"profiles": [len(collects)]}

    monkeypatch.setattr(gw, "_collect_profile_gateway_topology", collect)
    monkeypatch.setattr(gw, "_TOPOLOGY_CACHE", {"ts": 0.0, "data": None, "fn": None})
    assert gw._collect_profile_gateway_topology_cached() == {"profiles": [1]}
    assert gw._collect_profile_gateway_topology_cached() == {"profiles": [1]}
    snapshot_mod.invalidate_status()
    assert gw._collect_profile_gateway_topology_cached() == {"profiles": [2]}

    # A collect already running when the write lands answers its caller but is not kept as fresh.
    def collect_racing_a_write():
        collects.append(1)
        gw.invalidate_topology_cache()
        return {"profiles": ["pre-write"]}

    monkeypatch.setattr(gw, "_collect_profile_gateway_topology", collect_racing_a_write)
    gw.invalidate_topology_cache()
    assert gw._collect_profile_gateway_topology_cached() == {"profiles": ["pre-write"]}
    monkeypatch.setattr(gw, "_collect_profile_gateway_topology", collect)
    assert gw._collect_profile_gateway_topology_cached()["profiles"] != ["pre-write"]


@pytest.mark.parametrize("headers", ["token", "none", "basic"])
def test_every_caller_class_sees_from_the_snapshot_exactly_what_a_fresh_compute_shows(armed_route, headers):
    """The red-team risk for one shared snapshot: a redaction leak across caller classes (Desktop token,
    unverified, the iPhone's basic-auth relay). Armed and unarmed bodies are identical for each class."""
    from hermes_cli import web_server

    client = TestClient(web_server.app)
    sent = {"token": {web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN}, "none": {},
            "basic": {"authorization": "Basic aGVybWVzOnNlY3JldA=="}}[headers]
    # Warm the snapshot with a VERIFIED caller first, so an unverified caller is served the full body's copy.
    client.get("/api/status", headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN})
    from_snapshot = client.get("/api/status", headers=sent).json()
    snapshot_mod.STATUS_SNAPSHOT.disarm()
    fresh = client.get("/api/status", headers=sent).json()
    assert from_snapshot == fresh


@pytest.mark.parametrize("verified, signed_out", [(True, False), (False, False), (False, True)])
def test_snapshot_and_fresh_bodies_match_for_verified_unverified_and_signed_out_callers(
        armed_route, monkeypatch, verified, signed_out):
    from hermes_cli import web_server

    async def caller_verified(_request):
        return verified

    monkeypatch.setattr(status_router, "_status_caller_verified", caller_verified)
    monkeypatch.setattr(status_router, "_webapp_caller_signed_out", lambda _request, _verified: signed_out)
    client = TestClient(web_server.app)
    from_snapshot = [client.get("/api/status").json() for _ in range(2)]
    snapshot_mod.STATUS_SNAPSHOT.disarm()
    fresh = client.get("/api/status").json()
    assert from_snapshot == [fresh, fresh]
