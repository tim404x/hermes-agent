"""Live voice: a request that supersedes one cancelled during the cold agent build must still run.

Tim's iPhone Live mode, 25 Nov 2026 00:10Z: "Hermes, what's up" -> prompt.submit on a fresh chat (agent build
~20 s on a loaded box) -> "Hi, how are you doing" -> the client interrupts the busy turn, then submits. The
interrupt landed in the build wait, the new submit was queued behind it (``status: queued``), and the cancelled
thread returned without draining: no Hermes answer ever, and the app said "The previous request hasn't stopped
yet. Try again." on every later request. Live reproduction: ~/.hermes/work/voice-live-iphone-1125/busy_probe.py.
"""
import threading
import time
import types

from tui_gateway import server


def _session(**extra):
    return {
        "agent": None,
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": True,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


def _run_cancelled_build(monkeypatch, *, queue_newer: bool):
    ready = threading.Event()
    session = _session(agent_ready=ready)
    emitted, drained = [], []
    monkeypatch.setattr(server, "_AGENT_BUILD_WAIT_SLICE", 0.01)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr(server, "_session_info", lambda agent, s=None: {"running": bool((s or {}).get("running"))})
    monkeypatch.setattr(server, "_clear_inflight_turn", lambda s: s.pop("inflight_turn", None))

    def fake_drain(rid, sid, s):
        drained.append((s.get("queued_prompt") or {}).get("text"))
        drained.append(("cancel_latch", s.get("_turn_cancel_requested")))
        return True
    monkeypatch.setattr(server, "_drain_queued_prompt", fake_drain)
    runs = []
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: runs.append(a[3]))
    server._sessions["sid"] = session
    try:
        t = threading.Thread(target=server._run_after_agent_ready,
                             args=("rid", "sid", session, "Hermes, what's up?", None, None, None))
        t.start()
        time.sleep(0.05)
        with session["history_lock"]:  # session.interrupt during the build wait
            session["_turn_cancel_requested"] = True
            session["queued_prompt"] = None
            if queue_newer:  # the superseding prompt.submit: running is still True, so it queues
                session["queued_prompt"] = {"text": "Hi, how are you doing?"}
        time.sleep(0.05)
        session["agent"] = types.SimpleNamespace()
        ready.set()  # the cold build finishes
        t.join(5)
        assert not t.is_alive()
    finally:
        server._sessions.pop("sid", None)
    return session, emitted, drained, runs


def test_prompt_queued_behind_a_build_cancelled_turn_is_drained_after_the_build(monkeypatch):
    _session_unused, emitted, drained, runs = _run_cancelled_build(monkeypatch, queue_newer=True)
    assert runs == []  # the cancelled first request never runs
    assert drained[0] == "Hi, how are you doing?"  # the newer request does
    assert drained[1] == ("cancel_latch", False)  # and does not inherit the Stop that preceded it
    # the client is told the cancelled turn is over (idle), not left busy
    assert any(e[0] == "session.info" and e[2] == {"running": False} for e in emitted)


def test_plain_stop_during_build_drains_nothing_and_reports_idle(monkeypatch):
    session, emitted, drained, runs = _run_cancelled_build(monkeypatch, queue_newer=False)
    assert runs == [] and drained == []
    assert session["running"] is False
    assert any(e[0] == "error" for e in emitted)
    assert any(e[0] == "session.info" for e in emitted)
