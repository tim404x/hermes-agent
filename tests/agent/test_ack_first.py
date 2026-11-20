"""Ack first: a long turn a person typed opens with one short line from the model (``agent/ack_first.py``).

The note rides the turn's user-message channel only when the surface delivers interim text as its own
message, never for a bot's DM, never twice, and never in the system prompt.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.ack_first import ACK_FIRST_NOTE, consume_ack_first_note, stage_ack_first
from agent.turn_context import _merge_gateway_notes

from tests.agent.test_turn_context import _build, _FakeAgent, _stub_runtime_main  # autouse fixture


def _live(agent):
    agent.interim_assistant_callback = lambda text, **_kw: None
    return agent


def test_a_person_turn_with_live_interim_delivery_carries_the_note_outside_the_system_prompt():
    agent = _live(_FakeAgent())
    stage_ack_first(agent, "research the top 5 humanoid vendors", person_turn=True)

    ctx = _build(agent, user_message="research the top 5 humanoid vendors")

    assert ACK_FIRST_NOTE in ctx.plugin_user_context
    assert ctx.messages[-1]["content"] == "research the top 5 humanoid vendors"
    assert ctx.active_system_prompt == "SYSTEM"


def test_the_note_is_one_shot_per_turn():
    agent = _live(_FakeAgent())
    stage_ack_first(agent, "build it", person_turn=True)
    assert ACK_FIRST_NOTE in _build(agent, user_message="build it").plugin_user_context

    # The next turn on the same (cached) agent was never staged: no note.
    assert ACK_FIRST_NOTE not in (_build(agent, user_message="and now?").plugin_user_context or "")


def test_no_note_where_interim_text_is_not_delivered_as_its_own_message():
    # Cron, one-shots, bot peers, webhooks, held streams: the surface wires no interim callback.
    agent = _FakeAgent()
    agent.interim_assistant_callback = None
    stage_ack_first(agent, "check the inbox", person_turn=True)
    assert ACK_FIRST_NOTE not in (_build(agent, user_message="check the inbox").plugin_user_context or "")


def test_no_note_for_machinery_turns_or_bot_dms():
    for message, person in (
        ("[Background process finished] exit 0", False),
        ("Message from 🤖 Neo | Chief of Staff (@hermes): Tim's ask: build the heatmap", True),
        ([{"type": "text", "text": "Message from 🤖 Kepler | ML & Product (@kepler): done"}], True),
    ):
        agent = _live(SimpleNamespace(_ack_first_note=ACK_FIRST_NOTE))
        stage_ack_first(agent, message, person_turn=person)
        assert consume_ack_first_note(agent) == ""


def test_a_note_staged_before_the_turn_muted_or_held_its_interims_is_dropped():
    muted = SimpleNamespace(interim_assistant_callback=lambda *_a, **_k: None)
    stage_ack_first(muted, "pull the numbers", person_turn=True)
    muted.interim_assistant_callback = None
    assert consume_ack_first_note(muted) == ""

    held = SimpleNamespace(interim_assistant_callback=lambda *_a, **_k: None, _hold_stream=True)
    stage_ack_first(held, "pull the numbers", person_turn=True)
    assert consume_ack_first_note(held) == ""


def test_the_note_goes_last_after_the_gateway_notes():
    agent = _live(SimpleNamespace(_gateway_turn_context_notes="GATEWAY NOTE"))
    stage_ack_first(agent, "plan the trip", person_turn=True)
    merged = _merge_gateway_notes(agent, [{"role": "user", "content": "plan the trip"}], 0, "")
    assert merged.startswith("GATEWAY NOTE") and merged.endswith(ACK_FIRST_NOTE)


def test_the_note_asks_for_the_models_own_words_and_never_supplies_them():
    # The ack is the bot's own sentence: the note must not hand it canned filler to repeat.
    for canned in ('"On it"', "On it.", "Working on it"):
        assert canned not in ACK_FIRST_NOTE
    assert "own words" in ACK_FIRST_NOTE and "once" in ACK_FIRST_NOTE


def test_goal_continuations_heartbeats_and_bot_dms_written_by_hermes_get_no_ack():
    """Contract with the prompt writers: every machine-written prompt that reaches a surface as a plain
    user turn (no display kind on the desktop/phone path) is recognised as machinery."""
    from hermes_cli.goals import (
        CONTINUATION_PROMPT_TEMPLATE,
        CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE,
        CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE,
        KANBAN_GOAL_CONTINUATION_TEMPLATE,
    )
    from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
    from tools.bot_mode_dm import _ATTRIBUTION_RE

    machine = [
        CONTINUATION_PROMPT_TEMPLATE.format(goal="ship it"),
        CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE.format(goal="ship it", contract_block="- done: tests"),
        CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE.format(goal="ship it", subgoals_block="- a"),
        KANBAN_GOAL_CONTINUATION_TEMPLATE.format(reason="not done"),
        HEARTBEAT_PROMPT_TEMPLATE.format(interval="30m", prompt="check the build"),
        "Message from 🤖 Forge | Hardware Lead (@forge): quote is in",
    ]
    assert _ATTRIBUTION_RE.match(machine[-1])
    for prompt in machine:
        agent = _live(SimpleNamespace())
        stage_ack_first(agent, prompt, person_turn=True)
        assert consume_ack_first_note(agent) == "", prompt[:40]

    # A person who merely mentions these words still gets the ack.
    agent = _live(SimpleNamespace())
    stage_ack_first(agent, "Continuing toward the goal: check the heartbeat config please", person_turn=True)
    assert consume_ack_first_note(agent) == ACK_FIRST_NOTE
