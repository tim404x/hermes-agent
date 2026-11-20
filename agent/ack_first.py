"""Ack first: a long turn opens with one short line from the model, sent as its own message.

``display.interim_assistant_messages`` already delivers the text a model writes beside a tool call as
a separate message on every surface (gateway platforms, the TUI/desktop/phone ``message.interim``
event). Claude models rarely write any before their first tool call, so a person waits on a bare
"working" indicator. A surface that will deliver that text stages this note for a turn a person
typed; ``agent.turn_context`` consumes it once, on the user-message channel (never the system
prompt, so the cached prefix stays byte-stable). The note only asks: the words are the model's own.
"""

from __future__ import annotations

import re
from typing import Any

ACK_FIRST_NOTE = (
    "[Ack first: if this needs tools or more than a few seconds of work, open your response with one "
    "short line in your own words (what you are about to do, rough ETA), then make your tool calls in "
    "that same response. That line reaches the user at once as its own message. Write it once, before "
    "your first tool call only, with no narration before later calls. Skip it when you can answer "
    "directly or may stay silent. Your final answer must still stand on its own.]"
)

# Machine-written turns that reach a surface as a plain prompt get no ack: a bot's DM (the attribution
# ``tools.bot_mode_dm`` prefixes; the sending bot reads only the final reply), goal and Kanban
# continuations (``hermes_cli.goals``) and heartbeats (``hermes_cli.heartbeat``).
_MACHINE_PROMPT = re.compile(r"\s*(?:Message from [^\n]*?\(@[^)\s]+\): |\[Continuing toward |\[Heartbeat\b)")


def _leading_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        for part in user_message:
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                return part["text"]
    return ""


def stage_ack_first(agent: Any, user_message: Any, *, person_turn: bool) -> None:
    """Arm (or clear) this turn's note. Armed only for a turn a person typed whose interim text this
    surface delivers (``interim_assistant_callback`` wired); assigned every turn so a reused agent
    never replays an earlier turn's note."""
    armed = (person_turn and getattr(agent, "interim_assistant_callback", None) is not None
             and not _MACHINE_PROMPT.match(_leading_text(user_message)))
    agent._ack_first_note = ACK_FIRST_NOTE if armed else ""


def consume_ack_first_note(agent: Any) -> str:
    """Pop the staged note; empty when interim delivery went away since staging (a muted or held
    turn) because a line nobody receives as its own message is no ack."""
    note = getattr(agent, "_ack_first_note", "") or ""
    if note:
        agent._ack_first_note = ""
    if (not isinstance(note, str) or getattr(agent, "interim_assistant_callback", None) is None
            or getattr(agent, "_hold_stream", False)):
        return ""
    return note
