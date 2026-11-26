"""A stalled owner whose control-socket backlog is full is slow, not gone (26 Nov 2026).

5:22-5:25 AM PT: the gateway owner loop stalled ~90 s (a Kanban notification read a long chat's
whole compression lineage on the loop). Its asyncio control socket stopped accepting, the listen
backlog (asyncio default 100) filled with workers, cron firers and probes, and every further
``connect()`` failed at once with EAGAIN (``BlockingIOError``). Discovery read that as
``invalid_control_peer`` (a profile's own socket) or "not served" (a secondary served by the
multiplexer, then ``starting`` from the held lock), so ``WorkerRPC`` raised the final
``owner_unavailable`` instead of the redial-able ``owner_unresponsive``: 10 Kepler Kanban workers
died mid-stall and their cards were parked (gave_up). A full backlog must read like a control
timeout, an instant refusal must never spin a polling loop, and the owner listens with a deep
backlog.
"""
import asyncio
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest

# Linux AF_UNIX reports a full listen queue as EAGAIN; macOS answers ECONNREFUSED instead.
linux_only = pytest.mark.platforms("linux")


def _short_dir(prefix):
    """Socket paths cap at ~108 bytes: the runner's tmp_path can be longer."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=base if os.path.isdir(base) else None))
    path.chmod(0o700)
    return path


def _stalled_listener(home):
    """A real listener at ``<home>/gateway.sock`` that never accepts, its backlog filled."""
    path = home / "gateway.sock"
    old = os.umask(0o177)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
    finally:
        os.umask(old)
    listener.listen(1)
    queued = []
    for _ in range(16):  # fill the backlog until the kernel refuses with EAGAIN
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.setblocking(False)
        try:
            peer.connect(str(path))
        except BlockingIOError:
            peer.close()
            return listener, queued
        queued.append(peer)
    for peer in queued:
        peer.close()
    listener.close()
    pytest.skip("kernel never reported a full AF_UNIX backlog")
    raise AssertionError("unreachable")  # pytest.skip raises


@pytest.fixture
def stalled_owner():
    home = _short_dir("hb-own-")
    listener, queued = _stalled_listener(home)
    try:
        yield home
    finally:
        for peer in queued:
            peer.close()
        listener.close()
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def served_secondary(monkeypatch):
    """A named profile with no socket of its own, served by a stalled multiplexer root."""
    root = _short_dir("hb-root-")
    secondary = _short_dir("hb-sec-")
    listener, queued = _stalled_listener(root)
    import hermes_cli.gateway_multiplex_mode as mode
    import hermes_cli.gateway_runtime_multiplex as multiplex
    monkeypatch.setattr(multiplex, "multiplexer_root_for", lambda home: root)
    state = {"serves": None}  # None = boot settles it (the unset default flag)
    monkeypatch.setattr(mode, "host_serves_named_profile", lambda default_home, home: state["serves"])
    try:
        yield secondary, state
    finally:
        for peer in queued:
            peer.close()
        listener.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(secondary, ignore_errors=True)


@linux_only
def test_a_full_control_backlog_reads_as_a_control_timeout_not_an_invalid_peer(stalled_owner):
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    verdict = discover_gateway_endpoint(stalled_owner, timeout=1.0)

    assert (verdict.state, verdict.reason_code) == ("inaccessible", "control_timeout")


@linux_only
def test_a_worker_whose_owner_backlog_is_full_waits_it_out_instead_of_failing_final(stalled_owner):
    """``owner_unresponsive`` is a lost link the journal resends for ``RECONNECT_BUDGET_S``;
    ``owner_unavailable`` ended the turn at once."""
    from agent.runtime_session_store import WorkerLinkLost, WorkerRPC

    rpc = WorkerRPC(stalled_owner)
    rpc.endpoint = object()  # type: ignore[assignment]  # this worker already talks to that owner

    with pytest.raises(WorkerLinkLost, match="owner_unresponsive"):
        rpc._connection()


@linux_only
def test_a_served_secondarys_worker_waits_out_a_stalled_multiplexer(served_secondary):
    """The Kepler case: its home has no socket, the multiplexer root's backlog is full."""
    from agent.runtime_session_store import WorkerLinkLost, WorkerRPC
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    secondary, _state = served_secondary

    verdict = discover_gateway_endpoint(secondary, timeout=1.0)
    assert (verdict.state, verdict.reason_code) == ("inaccessible", "control_timeout")

    rpc = WorkerRPC(secondary)
    rpc.endpoint = object()  # type: ignore[assignment]
    with pytest.raises(WorkerLinkLost, match="owner_unresponsive"):
        rpc._connection()


@linux_only
def test_a_standalone_secondary_is_still_not_served_by_a_stalled_multiplexer(served_secondary):
    from hermes_cli.gateway_runtime import discover_gateway_endpoint
    secondary, state = served_secondary
    state["serves"] = False  # gateway.standalone: true

    verdict = discover_gateway_endpoint(secondary, timeout=1.0)

    assert verdict.reason_code != "control_timeout"
    assert verdict.state in {"absent", "starting", "inaccessible"}


def test_an_instant_control_timeout_cannot_spin_the_ensure_loop(tmp_path, monkeypatch):
    """EAGAIN returns ``control_timeout`` without spending the probe budget; ensure must pause."""
    import hermes_cli.gateway_runtime as runtime
    home = tmp_path / "home"
    home.mkdir()
    probes = []

    def instant_refusal(profile_home, *, timeout=2.0):
        if timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        probes.append(timeout)
        return runtime.GatewayDiscovery("inaccessible", reason_code="control_timeout")

    monkeypatch.setattr(runtime, "discover_gateway_endpoint", instant_refusal)
    monkeypatch.setattr(runtime, "_update_fenced", lambda homes: False)
    monkeypatch.setattr(runtime, "run_elsewhere_verdict", lambda home: None)

    import time
    started = time.monotonic()
    try:
        runtime.ensure_gateway_runtime(home, timeout=0.5)
    except ValueError:
        pass  # the deadline's zero-budget probe; either ending is fine

    assert time.monotonic() - started < 3.0
    assert 1 <= len(probes) <= 12  # ~one probe per 0.1 s pause, not thousands


@pytest.mark.platforms("posix")
def test_the_control_socket_listener_requests_a_deep_backlog(tmp_path, monkeypatch):
    from gateway import control_socket
    home = _short_dir("hb-srv-")
    requested = {}
    real = asyncio.start_unix_server

    async def spy(*args, **kwargs):
        requested.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(control_socket.asyncio, "start_unix_server", spy)

    async def scenario():
        server = control_socket.GatewayControlServer(home)
        assert await server.start()
        await server.stop()

    try:
        asyncio.run(scenario())
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert requested.get("backlog") == control_socket.CONTROL_SOCKET_BACKLOG >= 1024
