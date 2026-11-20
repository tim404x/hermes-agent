"""Swarm steer-safety contract: a mid-run message must never kill subagents.

With ``display.busy_input_mode: steer`` a message typed while the parent agent
is driving ``delegate_task`` children is injected via ``steer()`` (the gateway
also fans the steer out to the children, which is expected). It must NEVER
reach ``interrupt()`` on the parent or on any child, on any surface:

* Telegram / any gateway platform (``GatewayRunner._handle_active_session_busy_message``)
* Desktop app + Ink TUI via tui_gateway (busy ``prompt.submit`` and the ``session.steer`` RPC)
* Classic prompt_toolkit CLI (``CLITuiMixin._tui_enter_while_busy``)
* The agent itself (``InterruptControlMixin.steer`` only queues text)

Fall-through cases (image attachment, ``steer()`` returns False, ``steer()``
raises) must degrade to QUEUE, never to an interrupt.
"""

from __future__ import annotations

import queue
import threading
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tui_gateway import server

FALLTHROUGHS = ["image", "rejected", "raises"]


def _steer_spy(kind: str) -> MagicMock:
    if kind == "raises":
        return MagicMock(side_effect=RuntimeError("steer exploded"))
    return MagicMock(return_value=kind != "rejected")


def _swarm(kind: str = "ok"):
    """A parent driving two live delegate_task children; every verb is a spy."""
    children = []
    for _ in range(2):
        child = MagicMock(name="child")
        child.steer = _steer_spy(kind)
        children.append(child)
    parent = MagicMock(name="parent")
    parent.steer = _steer_spy(kind)
    parent._active_children = list(children)
    parent._active_children_lock = threading.Lock()
    parent._supports_active_turn_redirect = True
    parent.get_activity_summary.return_value = {"api_call_count": 3, "max_iterations": 60,
                                                "current_tool": "delegate_task"}
    return parent, children


def _assert_nothing_killed(parent, children):
    parent.interrupt.assert_not_called()
    parent.redirect.assert_not_called()
    for child in children:
        child.interrupt.assert_not_called()
        child.stop.assert_not_called()


# ── Telegram / gateway ────────────────────────────────────────────────────

def _gateway(tmp_path, monkeypatch, parent):
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    runner = GatewayRunner(config=GatewayConfig())
    runner._busy_input_mode = "steer"
    runner._session_state("key").turn.agent = parent
    adapter = MagicMock(_pending_messages={}, send=AsyncMock(), _send_with_retry=AsyncMock())
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_user_authorized_for_source = lambda source: True
    runner._interrupt_running_agent_for_busy_event = AsyncMock()
    queued = []
    real_queue = runner._queue_or_replace_pending_event
    runner._queue_or_replace_pending_event = lambda key, ev: (queued.append(ev), real_queue(key, ev))
    return runner, queued


def _tg_event(kind: str = "ok") -> MessageEvent:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="c", user_id="u", chat_type="dm")
    if kind == "image":
        return MessageEvent(text="look at this", message_type=MessageType.PHOTO, source=source,
                            message_id="m", media_urls=["/tmp/x.jpg"], media_types=["image/jpeg"])
    return MessageEvent(text="focus on rows 10-20", message_type=MessageType.TEXT, source=source, message_id="m")


@pytest.mark.asyncio
async def test_gateway_steer_reaches_parent_and_children_without_interrupt(tmp_path, monkeypatch):
    parent, children = _swarm()
    runner, queued = _gateway(tmp_path, monkeypatch, parent)

    assert await runner._handle_active_session_busy_message(_tg_event(), "key") is True

    parent.steer.assert_called_once()
    assert parent.steer.call_args.args[0].endswith("focus on rows 10-20")
    for child in children:  # fan-out to the looping children is expected
        child.steer.assert_called_once_with(parent.steer.call_args.args[0])
    assert queued == []  # steered text must not replay as a next turn
    runner._interrupt_running_agent_for_busy_event.assert_not_called()
    _assert_nothing_killed(parent, children)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", FALLTHROUGHS)
async def test_gateway_steer_fallthrough_queues_never_interrupts(kind, tmp_path, monkeypatch):
    parent, children = _swarm(kind)
    runner, queued = _gateway(tmp_path, monkeypatch, parent)
    event = _tg_event(kind)

    outcome = await runner._resolve_busy_steer_or_redirect(event, "key", "steer", parent)
    assert outcome.effective_mode == "queue" and not outcome.steered and not outcome.redirected
    assert await runner._handle_active_session_busy_message(event, "key") is True

    assert queued == [event]
    if kind == "image":
        parent.steer.assert_not_called()
    runner._interrupt_running_agent_for_busy_event.assert_not_called()
    _assert_nothing_killed(parent, children)


# ── Desktop + Ink TUI (tui_gateway) ───────────────────────────────────────

@pytest.fixture
def tui(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")
    monkeypatch.setattr(server, "_session_compression_in_flight", lambda session: False)
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *a, **k: None)
    kills = MagicMock()
    monkeypatch.setattr(server, "_interrupt_busy_session", kills)
    monkeypatch.setattr(server, "_interrupt_session_turn", kills)

    def make(parent, images=()):
        return {"agent": parent, "session_key": "session-key", "history": [], "history_lock": threading.Lock(),
                "history_version": 0, "running": True, "transport": None, "attached_images": list(images)}
    return make, kills


def test_tui_prompt_submit_steers_without_interrupt(tui):
    make, kills = tui
    parent, children = _swarm()
    resp = server._handle_busy_submit("r1", "sid", make(parent), "focus on rows 10-20", "ws-1")
    assert resp["result"]["status"] == "steered"
    parent.steer.assert_called_once_with("focus on rows 10-20")
    kills.assert_not_called()
    _assert_nothing_killed(parent, children)


@pytest.mark.parametrize("kind", FALLTHROUGHS)
def test_tui_prompt_submit_fallthrough_queues_never_interrupts(kind, tui):
    make, kills = tui
    parent, children = _swarm(kind)
    session = make(parent, images=["/tmp/x.png"] if kind == "image" else ())
    resp = server._handle_busy_submit("r1", "sid", session, "focus on rows 10-20", "ws-1")
    assert resp["result"]["status"] == "queued"
    assert session["queued_prompt"]["text"] == "focus on rows 10-20"
    kills.assert_not_called()
    _assert_nothing_killed(parent, children)


@pytest.mark.parametrize("kind, status", [("ok", "queued"), ("rejected", "rejected"), ("raises", None)])
def test_desktop_session_steer_rpc_never_interrupts(kind, status, tui, monkeypatch):
    make, kills = tui
    parent, children = _swarm(kind)
    monkeypatch.setitem(server._sessions, "sid", make(parent))
    resp = server.handle_request(
        {"id": "r1", "method": "session.steer", "params": {"session_id": "sid", "text": "focus on rows 10-20"}})
    if status is None:  # steer() raised: an error reply, the client keeps the text and queues it
        assert resp["error"]["code"] == 5000
    else:
        assert resp["result"]["status"] == status
    parent.steer.assert_called_once_with("focus on rows 10-20")
    kills.assert_not_called()
    _assert_nothing_killed(parent, children)


# ── Classic prompt_toolkit CLI ────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["ok", *FALLTHROUGHS])
def test_classic_cli_steer_never_interrupts(kind):
    from hermes_cli.cli_tui_mixin import CLITuiMixin

    parent, children = _swarm(kind)
    images = ["/tmp/x.png"] if kind == "image" else []
    payload = ("focus", images) if images else "focus"
    cli = types.SimpleNamespace(agent=parent, busy_input_mode="steer", _gateway_queue_store=lambda: None,
                                _pending_input=queue.Queue(), _interrupt_queue=queue.Queue())
    with patch("agent.onboarding.is_seen", lambda *a: True), patch("cli._cprint"):
        CLITuiMixin._tui_enter_while_busy(cli, "focus", images, payload, mode="steer")

    assert cli._interrupt_queue.empty()  # the interrupt queue is what triggers agent.interrupt()
    if kind == "ok":
        parent.steer.assert_called_once_with("focus")
        assert cli._pending_input.empty()
    else:
        assert cli._pending_input.get_nowait() == payload
    _assert_nothing_killed(parent, children)


# ── The agent itself ──────────────────────────────────────────────────────

def test_agent_steer_only_queues_text_never_interrupts_children():
    from agent.interrupt_control import InterruptControlMixin

    class _Parent(InterruptControlMixin):
        pass

    parent = _Parent()
    _, children = _swarm()
    parent._active_children, parent._active_children_lock = list(children), threading.Lock()
    parent._pending_steer_lock, parent._pending_steer = threading.Lock(), None
    parent.interrupt = MagicMock()

    assert parent.steer("focus on rows 10-20") is True
    assert parent._pending_steer == "focus on rows 10-20"
    parent.interrupt.assert_not_called()
    for child in children:
        child.interrupt.assert_not_called()
        child.stop.assert_not_called()
        child.steer.assert_not_called()  # fan-out is the surface's job, not steer()'s
