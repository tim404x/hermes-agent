"""Desktop Bot Chat and the phone app (TUI gateway) arm the ack-first note only for a prompt the person typed.

The signal is positive: a turn the client's ``prompt.submit`` started (``_run_after_agent_ready``, the
RPC's turn thread) carries ``person_turn`` into ``_run_prompt_submit``, which hands it to
``_invoke_agent``. Every wake the backend synthesizes re-enters ``_run_prompt_submit`` without it,
whatever its display kind: a Kanban notification batch, a process ``watch_match`` event, a ``/loop``
wakeup tick (both templates), a heartbeat tick and a hosted Group Chat member turn. A relayed bot's
message and a chat with interim messages off get no ack either.
"""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

import pytest

import tui_gateway.server as srv
from agent.ack_first import ACK_FIRST_NOTE, consume_ack_first_note

TYPED = "find me 3 venues near SoMa for 40 people"
_RUN_PROMPT_SUBMIT = srv._run_prompt_submit  # the real one; ``submits`` replaces it to capture callers


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _live_session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else SimpleNamespace(), "session_key": "gw-session-key", "history": [],
        "history_lock": threading.Lock(), "history_version": 0, "running": False, "attached_images": [],
        "image_counter": 0, "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "inflight_turn": None, "transport": None, **extra,
    }


@pytest.fixture
def note_for(monkeypatch, tmp_path):
    """The note the real ``_run_prompt_submit`` arms for a turn submitted with these arguments (it reaches
    ``run_conversation`` through ``_invoke_agent``)."""
    monkeypatch.setattr(srv.threading, "Thread", _InlineThread)
    for name in ("_emit", "_wire_callbacks", "_sync_agent_model_with_config", "_register_session_cwd",
                 "_tts_stream_begin", "_sync_session_key_after_compress"):
        monkeypatch.setattr(srv, name, lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_session_cwd", lambda _session: str(tmp_path))
    monkeypatch.setattr(srv, "_get_usage", lambda _agent: {})

    def _note(text, *, interim_on=True, **submit_kwargs):
        monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: interim_on)
        seen = {}

        def run_conversation(_message, **_kwargs):
            seen["note"] = consume_ack_first_note(agent)
            return {"final_response": "ok"}

        # A note an earlier turn left on the reused agent must not survive either.
        agent = SimpleNamespace(session_id="a", run_conversation=run_conversation, clear_interrupt=lambda: None,
                                _ack_first_note=ACK_FIRST_NOTE)
        _RUN_PROMPT_SUBMIT("rid", "ui-sid", _live_session(agent=agent, running=True, session_key="ack-turn"), text,
                           **submit_kwargs)
        return seen["note"]

    return _note


@pytest.fixture
def submits(monkeypatch):
    """Every ``_run_prompt_submit`` call a wake or the RPC thread makes: ``(text, kwargs)``."""
    calls = []

    def fake_submit(_rid, _sid, _session, text, **kwargs):
        calls.append((text, kwargs))
        return True

    monkeypatch.setattr(srv, "_run_prompt_submit", fake_submit)
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: None)
    return calls


def _session():
    return {"session_key": "k", "history_lock": contextlib.nullcontext(), "running": False,
            "agent": SimpleNamespace()}


def _only_submit(submits, note_for):
    assert len(submits) == 1, submits
    text, kwargs = submits[0]
    return note_for(text, **kwargs)


def _rpc_turn(monkeypatch, text, hosted_terminal_callback=None, **kwargs):
    """The turn thread ``prompt.submit`` starts once the agent is ready."""
    monkeypatch.setattr(srv, "_wait_agent_for_prompt", lambda *_a: None)
    session = _session()
    session["running"] = True
    srv._run_after_agent_ready("rid", "sid", session, text, None, None, hosted_terminal_callback, **kwargs)


def test_a_prompt_the_person_typed_arms_the_ack(monkeypatch, submits, note_for):
    _rpc_turn(monkeypatch, TYPED)
    assert _only_submit(submits, note_for) == ACK_FIRST_NOTE


def test_a_hosted_group_chat_member_turn_gets_no_ack(monkeypatch, submits, note_for):
    # The room worker submits each member's turn through prompt.submit with ``_hosted_task`` and its
    # terminal callback (tui_gateway.hosted_room_server_rpc).
    _rpc_turn(monkeypatch, "@forge the new rig quote?", hosted_terminal_callback=lambda _receipt: None)
    assert _only_submit(submits, note_for) == ""


def test_a_relayed_bot_message_gets_no_ack(monkeypatch, submits, note_for):
    _rpc_turn(monkeypatch, "status of the Figure export?", turn_author={"id": "atlas", "name": "Atlas"})
    assert _only_submit(submits, note_for) == ""


def test_a_kanban_notification_batch_gets_no_ack(monkeypatch, submits, note_for):
    monkeypatch.setattr(srv, "_session_turn_admission", lambda _s: contextlib.nullcontext(True))
    monkeypatch.setattr(srv, "_collect_kanban_notifications",
                        lambda _s: ["✔ [default] @kepler Kanban t_abc123 done: Build the Figure heatmap"])
    srv._notif_poll_kanban_scoped("sid", _session())
    assert _only_submit(submits, note_for) == ""


def test_a_process_watch_match_event_gets_no_ack(monkeypatch, submits, note_for):
    import tools.async_delegation as ad
    from tools.process_registry_notifications import format_process_notification

    monkeypatch.setattr(ad, "claim_event_delivery", lambda _evt, _who: object())
    monkeypatch.setattr(ad, "complete_event_delivery", lambda *_a, **_k: None)
    monkeypatch.setattr(ad, "release_event_delivery", lambda *_a, **_k: None)
    evt = {"type": "watch_match", "session_id": "proc_1", "command": "tail -f build.log", "pattern": "ERROR",
           "output": "ERROR boom"}
    session = _session()
    session["running"], session["_notif_turn_claim"] = True, object()
    srv._notif_dispatch_event("sid", session, evt, format_process_notification(evt))
    assert _only_submit(submits, note_for) == ""


@pytest.mark.parametrize("template", ["WAKEUP_PROMPT_TEMPLATE", "WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE"])
def test_a_loop_wakeup_tick_gets_no_ack(monkeypatch, submits, note_for, template):
    from hermes_cli import loops

    wake = getattr(loops, template).format(tick=3, cadence=" every 10m", prompt="check the deploy; stay quiet if green",
                                           until="it is green")

    class _Loop:
        def __init__(self, session_id):
            self.state = SimpleNamespace(ticks_fired=3)

        def is_due(self):
            return True

        def fire_tick(self):
            return wake

    monkeypatch.setattr(srv, "_session_turn_admission", lambda _s: contextlib.nullcontext(True))
    monkeypatch.setattr(loops, "goal_blocks_loop_tick", lambda _key: False)
    monkeypatch.setattr(srv, "_loop_route_is_gateway_chat", lambda _state: False)
    monkeypatch.setattr(srv, "_publish_session_control_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_notif_loop_status", lambda *_a, **_k: None)
    real_loop_manager = loops.LoopManager
    monkeypatch.setattr(loops, "LoopManager", _Loop)
    srv._maybe_fire_tui_loop_tick("sid", _session())
    monkeypatch.setattr(loops, "LoopManager", real_loop_manager)  # the replayed turn's own loop hooks
    assert _only_submit(submits, note_for) == ""


def test_a_heartbeat_tick_gets_no_ack(monkeypatch, submits, note_for):
    from hermes_cli import heartbeat

    class _Heartbeat:
        def __init__(self, session_id):
            self.state = SimpleNamespace(is_due=lambda: True, fire_count=2)

        def is_active(self):
            return True

        def due_prompt(self):
            return heartbeat.HEARTBEAT_PROMPT_TEMPLATE.format(interval="30m", prompt="check the inbox")

    monkeypatch.setattr(srv, "_session_turn_admission", lambda _s: contextlib.nullcontext(True))
    monkeypatch.setattr(srv, "_notif_gateway_owns_heartbeat", lambda *_a: False)
    real_heartbeat_manager = heartbeat.HeartbeatManager
    monkeypatch.setattr(heartbeat, "HeartbeatManager", _Heartbeat)
    srv._maybe_fire_tui_heartbeat_tick("sid", _session())
    monkeypatch.setattr(heartbeat, "HeartbeatManager", real_heartbeat_manager)
    assert _only_submit(submits, note_for) == ""


def test_no_ack_for_a_display_kinded_turn_or_with_interim_messages_off(note_for):
    assert note_for(TYPED, person_turn=True, display_kind="hidden") == ""
    assert note_for(TYPED, person_turn=True, interim_on=False) == ""
    assert note_for("[process finished] exit 0", display_kind="process_complete") == ""
