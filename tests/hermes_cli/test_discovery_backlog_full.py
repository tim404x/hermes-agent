"""A stalled owner whose control-socket backlog is full is slow, not gone (26 Nov 2026).

5:22-5:25 AM PT: the gateway owner loop stalled ~90 s (a Kanban notification read a long chat's
whole compression lineage on the loop). Its asyncio control socket stopped accepting, the listen
backlog (asyncio default 100) filled with workers, cron firers and probes, and every further
``connect()`` failed at once with EAGAIN (``BlockingIOError``). ``discover_gateway_endpoint`` read
that as ``invalid_control_peer``, so ``WorkerRPC`` raised the final ``owner_unavailable`` instead of
the redial-able ``owner_unresponsive``: 10 Kepler Kanban workers died mid-stall and their cards
were parked (gave_up). A full backlog must read like a control timeout, and the owner listens
with a deep backlog.
"""
import os
import socket
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX control socket only")


@pytest.fixture
def stalled_owner(tmp_path):
    """A real listener at ``<home>/gateway.sock`` that never accepts, its backlog filled."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = home / "gateway.sock"
    old = os.umask(0o177)
    try:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
    finally:
        os.umask(old)
    listener.listen(1)
    queued = []
    try:
        for _ in range(16):  # fill the backlog until the kernel refuses with EAGAIN
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.setblocking(False)
            try:
                peer.connect(str(path))
            except BlockingIOError:
                peer.close()
                break
            queued.append(peer)
        else:
            pytest.skip("kernel never reported a full AF_UNIX backlog")
        yield home
    finally:
        for peer in queued:
            peer.close()
        listener.close()


def test_a_full_control_backlog_reads_as_a_control_timeout_not_an_invalid_peer(stalled_owner):
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    verdict = discover_gateway_endpoint(stalled_owner, timeout=1.0)

    assert (verdict.state, verdict.reason_code) == ("inaccessible", "control_timeout")


def test_a_worker_whose_owner_backlog_is_full_waits_it_out_instead_of_failing_final(stalled_owner):
    """``owner_unresponsive`` is a lost link the journal resends for ``RECONNECT_BUDGET_S``;
    ``owner_unavailable`` ended the turn at once."""
    from agent.runtime_session_store import WorkerLinkLost, WorkerRPC

    rpc = WorkerRPC(stalled_owner)
    rpc.endpoint = object()  # this worker already talks to that owner

    with pytest.raises(WorkerLinkLost, match="owner_unresponsive"):
        rpc._connection()


def test_the_control_socket_listens_with_a_deep_backlog():
    from gateway import control_socket

    assert control_socket.CONTROL_SOCKET_BACKLOG >= 1024
