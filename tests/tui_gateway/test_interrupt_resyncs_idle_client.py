"""session.interrupt on a session that is not running must re-announce the idle state (25 Nov 2026).

Tim's iPhone Live mode: the app's busy flag outlived the turn (no idle event ever reached it), so on each new
spoken request it interrupted the "busy" chat, got ``{"status": "interrupted"}`` and no event, and refused the
request with "The previous request hasn't stopped yet. Try again." None of his requests reached Hermes. An
interrupt that finds nothing running now emits ``session.info`` (running false) so any stale client resyncs, and
a turn still unwinding keeps emitting its own idle state.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server


def record(sid: str, **state) -> dict:
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "agent_error": None,
        "history": [],
        "history_lock": threading.RLock(),
        "running": False,
        "session_key": sid,
        **state,
    }
    server._sessions[sid] = session
    return session


@pytest.fixture
def sid(request, monkeypatch):
    sid = f"resync-{request.node.name}"
    emitted: list = []
    monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr(server, "_session_info", lambda agent, s=None: {"running": bool((s or {}).get("running"))})
    yield sid, emitted
    server._sessions.pop(sid, None)


def interrupt(sid: str) -> dict:
    return server._methods["session.interrupt"](1, {"session_id": sid})


def test_interrupt_of_an_idle_chat_reannounces_idle(sid):
    sid, emitted = sid
    record(sid)

    response = interrupt(sid)

    assert response["result"] == {"status": "interrupted"}
    infos = [a for a in emitted if a[0] == "session.info" and a[1] == sid]
    assert infos and infos[-1][2]["running"] is False


def test_interrupt_of_a_failed_build_turn_reannounces_idle(sid):
    """Stop frees a running session whose build failed: the client must hear that it is idle now."""
    sid, emitted = sid
    session = record(sid, agent_build_started=True, agent_error="boom", running=True)
    session["agent_ready"].set()

    response = interrupt(sid)

    assert response["result"]["status"] == "interrupted"
    assert session["running"] is False
    assert any(a[0] == "session.info" and a[1] == sid and a[2]["running"] is False for a in emitted)


def test_interrupt_of_a_live_turn_leaves_idle_to_the_turn(sid):
    """A turn that is still unwinding emits its own idle state; the interrupt must not claim idle early."""
    sid, emitted = sid
    session = record(sid, agent_build_started=True, running=True,
                     _run_thread=types.SimpleNamespace(is_alive=lambda: True))

    response = interrupt(sid)

    assert response["result"]["status"] == "interrupted"
    assert session["running"] is True
    assert not any(a[0] == "session.info" and a[1] == sid and a[2]["running"] is False for a in emitted)
