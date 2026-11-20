"""Ack first: a long turn opens with one short line from the model, sent as its own message.

``display.interim_assistant_messages`` already delivers the text a model writes beside a tool call as
a separate message on every surface (gateway platforms, the TUI/desktop/phone ``message.interim``
event). Claude models rarely write any before their first tool call, so a person waits on a bare
"working" indicator. A surface that will deliver that text stages this note for a turn a person
typed; ``agent.turn_context`` consumes it once, on the user-message channel (never the system
prompt, so the cached prefix stays byte-stable). The note only asks: the words are the model's own.
The shared soul carries the full rule, so the note is a short recency nudge (it replays with every
armed historical turn).

The note is API-only on every path. A text turn carries it in the ``api_content`` sidecar. A
multimodal (list) turn holds it under ``API_TAIL_NOTE`` on the live message: each request adds it
back (``with_api_only_note``) and the stored row keeps it only in its ``api_content`` sidecar
(``list_api_content``), never in ``content``, which feeds transcripts, search, exports and
compaction.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from agent.message_metadata import API_TAIL_NOTE
from session_rules.turns import _durable_content

ACK_FIRST_NOTE = (
    "[If this takes several tool calls or ~20+ s, open with one short line on what you'll do; "
    "skip it for a quick lookup.]"
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


def with_api_only_note(parts: list, note: str) -> list:
    """The parts a request sends for a list user message holding an API-only note: the note joins the
    trailing text part, or follows a trailing image as its own part. That is exactly the shape a replay
    of the row's text projection rebuilds (``agent.image_replay``), so the request bytes for that turn
    are the same live, warm and rebuilt from the store."""
    last = parts[-1] if parts else None
    if isinstance(last, dict) and last.get("type") == "text" and isinstance(last.get("text"), str):
        return [*parts[:-1], {**last, "text": last["text"] + "\n\n" + note}]
    return [*parts, {"type": "text", "text": note}]


def list_api_content(message: Mapping[str, Any]) -> Optional[str]:
    """The stored row's ``api_content`` sidecar for a list user message holding an API-only note: the
    text projection of what the request sent. ``None`` without such a note."""
    content, note = message.get("content"), message.get(API_TAIL_NOTE)
    if not (isinstance(content, list) and isinstance(note, str) and note):
        return None
    return _durable_content(with_api_only_note(content, note))
