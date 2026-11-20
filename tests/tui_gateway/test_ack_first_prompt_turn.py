"""Desktop Bot Chat and the phone app (TUI gateway) arm the ack-first note for a prompt the person typed.

``_invoke_agent`` wires ``interim_assistant_callback`` (sealed by the desktop and the phone as its own
bubble); a synthesized turn (queued drain, crash auto-continue, notification) carries a display kind
and gets no ack, nor does a chat with interim messages off.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import tui_gateway.server as srv
from agent.ack_first import ACK_FIRST_NOTE, consume_ack_first_note


@pytest.fixture
def run_turn(monkeypatch):
    monkeypatch.setattr(srv, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (
        SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def _run(prompt, *, display_kind=None, interim_on=True, stale=""):
        monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: interim_on)
        seen = {}

        def run_conversation(_message, **_kwargs):
            seen["note"] = consume_ack_first_note(agent)
            return {"final_response": "done"}

        agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation,
                                _ack_first_note=stale)
        session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(),
                   "agent": agent}
        st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
        srv._invoke_agent("sid", session, st, prompt, prompt, None, [], display_kind, None)
        return seen["note"]

    return _run


def test_a_typed_prompt_arms_the_ack(run_turn):
    assert run_turn("find me 3 venues near SoMa for 40 people") == ACK_FIRST_NOTE


def test_no_ack_for_synthesized_turns_bot_dms_or_with_interim_messages_off(run_turn):
    # Each also clears a note an earlier turn left on the reused agent.
    assert run_turn("[process finished] exit 0", display_kind="process_complete", stale=ACK_FIRST_NOTE) == ""
    assert run_turn("Message from 🤖 Atlas | Growth Lead (@atlas): status?", stale=ACK_FIRST_NOTE) == ""
    assert run_turn("find me 3 venues", interim_on=False, stale=ACK_FIRST_NOTE) == ""
