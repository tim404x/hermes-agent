"""A prompt sent right after Stop must queue behind the stopped turn, never steer into it (25 Nov 2026).

Live voice (the iPhone app, desktop) supersedes a request with a new one: session.interrupt on the busy turn, then
prompt.submit. With busy_input_mode=steer the new text was injected into the turn that was already unwinding and was
lost with it: no answer, and the app reported "The previous request hasn't stopped yet". Guard probe:
par-voicephone/bin/phone_supersede_probe.py.
"""
import threading
import types

from tui_gateway import server


def _session(**extra):
    return {"agent": None, "session_key": "k", "history": [], "history_lock": threading.RLock(), "history_version": 0,
            "running": True, "attached_images": [], **extra}


def _agent(calls):
    return types.SimpleNamespace(steer=lambda text: calls.append(("steer", text)) or True,
                                 redirect=lambda text: calls.append(("redirect", text)) or True,
                                 _supports_active_turn_redirect=True, interrupt=lambda *a, **k: None)


def test_submit_after_stop_is_queued_not_steered(monkeypatch):
    calls = []
    session = _session(agent=_agent(calls), _turn_cancel_requested=True)
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *a, **k: None)
    resp = server._handle_busy_submit("r1", "sid", session, "Hi, how are you doing?", None)
    assert resp["result"]["status"] == "queued"
    assert calls == []
    assert session["queued_prompt"]["text"] == "Hi, how are you doing?"


def test_submit_during_a_live_turn_still_steers(monkeypatch):
    calls = []
    session = _session(agent=_agent(calls))
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *a, **k: None)
    resp = server._handle_busy_submit("r1", "sid", session, "also check the deck", None)
    assert resp["result"]["status"] == "steered"
    assert calls == [("steer", "also check the deck")]
