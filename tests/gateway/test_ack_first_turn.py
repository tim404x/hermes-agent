"""The gateway arms the ack-first note only for a person's message whose interim text this chat delivers.

``TurnRunner._wire_turn_agent_callbacks`` wires the turn's callbacks; a chat that sends interim
assistant text as its own message (Telegram, Slack, desktop side windows) gets the model's one-line
ack on a long turn. A self-injected notification or heartbeat, a muted turn, an API client and a
chat with interim messages off do not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.ack_first import ACK_FIRST_NOTE, consume_ack_first_note
from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner


def _turn_runner(*, platform=Platform.TELEGRAM, display_kind=None, reply_expected=None, muted=False):
    ctx = MagicMock()
    ctx.source = SimpleNamespace(platform=platform, chat_id="42", is_bot=False)
    ctx.message = "pull Figure's hours for October and chart them"
    ctx.persist_user_display_kind = display_kind
    ctx.reply_expected = reply_expected
    ctx.mute_notification_reply = muted
    ctx.user_config = {}
    ctx._voice_ack_guild = [None]
    ctx._native_slack_task_cards = False
    ctx._hooks_ref = SimpleNamespace(loaded_hooks=[])
    ctx._status_adapter = None
    ctx.voice_turn = False
    ctx.agent_holder = [None]
    ctx.tools_holder = [None]
    runner = SimpleNamespace(
        _consume_pending_turn_sidecar_notes=lambda _key: (),
        _load_daybreak=lambda _source: False,
        _daybreak_surface=lambda _source: False,
    )
    turn = object.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = ctx
    turn._approval_owner = None
    turn._make_bg_review_callbacks = lambda: (None, None)
    turn._attach_session_title_callback = lambda _agent, _ctx: None
    return turn


def _wire(turn, *, want_interim=True, stale=""):
    agent = SimpleNamespace(_ack_first_note=stale)
    turn._wire_turn_agent_callbacks(agent, {}, None, None, lambda *_a, **_k: None, want_interim)
    return consume_ack_first_note(agent)


@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.SLACK, Platform.LOCAL])
def test_a_persons_message_with_interim_delivery_arms_the_ack(platform):
    assert _wire(_turn_runner(platform=platform)) == ACK_FIRST_NOTE


@pytest.mark.parametrize("kwargs, want_interim", [
    ({"display_kind": "internal_notification"}, True),  # background/delegation notice, heartbeat
    ({"reply_expected": False}, True),
    ({"muted": True}, True),
    ({"platform": Platform.API_SERVER}, True),
    ({}, False),  # interim messages off, a bot peer, a webhook, a one-shot or a held stream
])
def test_no_ack_where_nobody_waits_on_it_or_it_would_not_arrive_alone(kwargs, want_interim):
    # A note left on a reused agent by an earlier turn is cleared too.
    assert _wire(_turn_runner(**kwargs), want_interim=want_interim, stale=ACK_FIRST_NOTE) == ""


def test_a_bot_dm_into_a_chat_gets_no_ack():
    turn = _turn_runner(platform=Platform.LOCAL)
    turn._ctx.message = "Message from 🤖 Neo | Chief of Staff (@hermes): Tim's ask: review the model"
    assert _wire(turn, stale=ACK_FIRST_NOTE) == ""
