"""Read-side display typing for machine rows the transcript stores as plain rows.

Both history projections (``tui_gateway/session_history.py::_history_to_messages`` for ``session.resume`` and
``hermes_cli/web_routers/sessions.py::_project_for_display`` for the REST page the desktop prefetches) run every
stored row through :func:`type_display_row`, so rows written before their writer typed them render the way new
rows do. Render time only: nothing here writes the database.

- a background-process completion (``process_complete``) shows a job title, never its shell command
  (:func:`tools.process_registry_notifications.relabel_legacy_process_title`);
- a cron output delivered into a Bot Chat (``[Cronjob "<name>" output ...]`` frame on a typed ``user`` row, or the
  writer's exact frame on a legacy untyped one) carries
  ``display_metadata.cron_delivery = {"job_name"}`` and its output as the display text, so it is never painted as
  the user's own message;
- an assistant row that is only an information-free interrupt sentinel (``Operation interrupted: waiting for model
  response (0.3s elapsed).``) keeps its row and its ``interrupted`` mark with no prose; any other text stays.

:func:`mark_bot_brief` types the seed prompt a bot wrote into a side window it opened for itself (``bot_brief``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

PROCESS_COMPLETE_DISPLAY_KIND = "process_complete"
BOT_BRIEF_DISPLAY_KIND = "bot_brief"
# Machine user rows a side window can carry before its first written prompt (notices, wakes, scaffolding).
_MACHINE_USER_KINDS = frozenset({"internal_notification", "process_complete", "async_delegation_complete", "hidden",
                                 "auto_continue"})
_BRIEF_SCAN_ROWS = 40
_CRON_FRAME_KINDS = (None, "", "internal_notification")

# The runtime's information-free interrupt sentinels, and only those (agent/conversation_loop.py
# INTERRUPT_WAITING_FOR_MODEL_PREFIX, turn_recovery_autorecover.py, turn_api_error.py retry, turn_empty_response.py):
# cancellation metadata, never prose. A sentinel that carries an error (``handling API error (...)``, ``during retry
# (<hint>, ...)``) or any other ``Operation interrupted ...`` sentence is the only copy of what went wrong and stays
# visible. Twin: apps/desktop/src/lib/chat-messages/machine-rows.ts, both pinned by
# tests/fixtures/interrupt_sentinel_cases.json.
_INTERRUPT_SENTINEL_RE = re.compile(
    r"^Operation interrupted: (?:waiting for model response \([0-9.]+s elapsed\)"
    r"|waiting for the provider to recover \(cycle \d+/\d+\)"
    r"|retrying API call after error \(retry \d+/\d+\)"
    r"|retrying empty response from model \(retry \d+/\d+\))\.$")
# The exact frame cron/scheduler_delivery.py::bot_chat_message opens a Bot Chat delivery with. An UNTYPED user row is
# read as a cron delivery only when it carries this writer wording (every legacy untyped delivery does); a person
# pasting or quoting a frame keeps their own message. Twin: machine-rows.ts, pinned by tests/fixtures/cron_frame_cases.json.
_LEGACY_CRON_FRAME_RE = re.compile(r'^\[Cronjob "[^"\n]*" output \u2014 scheduled job, not the user\.')


def is_interrupt_sentinel(text: Any) -> bool:
    """Whether *text* is nothing but an ``Operation interrupted: ...`` cancellation sentinel."""
    return isinstance(text, str) and bool(_INTERRUPT_SENTINEL_RE.match(text.strip()))


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content
                         if isinstance(part, Mapping) and part.get("type") in ("text", "input_text"))
    return ""


def type_display_row(row: Mapping[str, Any]) -> dict:
    """*row* (a stored message dict: ``role``, ``content``, ``display_kind``, ``display_metadata``) with its display
    typing applied: a new dict when anything changed, else *row* itself. ``display_content`` is the text a surface
    shows instead of ``content`` (``content`` is kept for export and the model-facing view)."""
    role, kind = row.get("role"), row.get("display_kind")
    meta = row.get("display_metadata") if isinstance(row.get("display_metadata"), Mapping) else None
    if kind == PROCESS_COMPLETE_DISPLAY_KIND and meta and isinstance(meta.get("display_text"), str):
        from tools.process_registry_notifications import relabel_legacy_process_title
        title = relabel_legacy_process_title(meta["display_text"])
        if title != meta["display_text"]:
            return {**row, "display_metadata": {**meta, "display_text": title}}
    if role == "user" and kind in _CRON_FRAME_KINDS:
        from cron.scheduler_delivery import parse_bot_chat_frame
        text = _text(row.get("content"))
        # Machine provenance first: the producer types its deliveries (internal_notification); an untyped row must
        # carry the writer's own frame wording, else it is the person's message.
        frame = parse_bot_chat_frame(text) if kind or _LEGACY_CRON_FRAME_RE.match(text) else None
        if frame is not None:
            job_name, output = frame
            return {**row, "display_kind": kind or "internal_notification", "display_content": output,
                    "display_metadata": {**(meta or {}), "cron_delivery": {"job_name": job_name},
                                         "display_text": job_name}}
    if role == "assistant" and is_interrupt_sentinel(row.get("content")):
        return {**row, "content": "", "display_content": "",
                "display_metadata": {**(meta or {}), "interrupted": True}}
    return row if isinstance(row, dict) else dict(row)


def bot_brief_author(author_id: Any, profile_root: Any = None) -> Optional[dict]:
    """``{"id", "name"}`` for a bot author id (``bot:<profile>``), the name in its short form (``Neo`` for a bot
    titled ``Neo | Chief of Staff``) when the local profile resolves, else the id; None for a non-bot id."""
    author_id = str(author_id or "").strip()
    if not author_id.startswith("bot:"):
        return None
    profile = author_id[len("bot:"):]
    return {"id": author_id, "name": _bot_short_name(profile) or author_id}


def _bot_short_name(profile: str) -> str:
    if not profile or "/" in profile:  # a bot on another install: no local profile to read
        return ""
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_mode_probe import _friendly_names

    try:
        profile_dir = get_profile_dir(profile)
    except ValueError:  # not a valid profile id: no local profile to read
        return ""
    name = next((n for n in _friendly_names(profile_dir) if n), "")
    return name.split(" | ", 1)[0].strip()


def is_bot_brief_candidate(row: Mapping[str, Any]) -> bool:
    """Whether *row* can be a bot's self-seeded brief: an untyped user row that is not an attributed DM."""
    return (row.get("role") == "user" and not row.get("display_kind")
            and not _text(row.get("content")).lstrip().startswith("Message from "))


def bot_brief_row(row: Mapping[str, Any], author: Mapping[str, Any]) -> dict:
    """*row* typed as the brief *author* (``{"id", "name"}``) seeded its side window with."""
    meta = row.get("display_metadata")
    return {**row, "display_kind": BOT_BRIEF_DISPLAY_KIND,
            "display_metadata": {**(dict(meta) if isinstance(meta, Mapping) else {}),
                                 "author": {"id": author["id"], "name": author["name"]}}}


def self_brief_author(turn_author: Any, own_profile: Optional[str], text: Any) -> Optional[dict]:
    """The ``{"id", "name"}`` to type a turn's user row ``bot_brief`` with, when a bot wrote it into its OWN
    profile (a task tab it opened for itself through ``bot_mode_dm.py --run-delivery --author bot:<self>``); None for
    a human turn, a teammate's DM, or an attributed ``Message from ...`` delivery."""
    if not isinstance(turn_author, Mapping) or not turn_author.get("is_bot") or not own_profile:
        return None
    if turn_author.get("id") != f"bot:{own_profile}" or _text(text).lstrip().startswith("Message from "):
        return None
    return bot_brief_author(turn_author["id"])


def _submitted_turn_author(store: Any, row: Mapping[str, Any]) -> Optional[dict]:
    """The bot author the row's own admission recorded (``display_metadata.submission_id`` -> the admission's
    ``cli_turn_v1.turn_author``), else None. Row-level provenance: who opened a tab, or stamped it, says nothing about
    who wrote its first message."""
    meta = row.get("display_metadata")
    if isinstance(meta, str):
        import json
        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    submission = meta.get("submission_id") if isinstance(meta, Mapping) else None
    if not isinstance(submission, str) or not submission:
        return None
    for admission in store.admissions.match(request_id=submission, include_retired=True):
        payload = admission.payload if isinstance(admission.payload, Mapping) else {}
        turn = payload.get("cli_turn_v1") if isinstance(payload.get("cli_turn_v1"), Mapping) else {}
        author = turn.get("turn_author") if isinstance(turn.get("turn_author"), Mapping) else None
        if author and author.get("is_bot"):
            return bot_brief_author(author.get("id"))
    return None


def find_bot_brief(store: Any, session_id: str) -> Optional[dict]:
    """``{"row_id", "author"}`` of the brief a bot seeded the side window *session_id* with: the lineage's first
    written user row is an untyped prompt (not an attributed ``Message from ...`` DM) whose own admission names a bot
    author. The side-window stamp only scopes the lookup to tabs; it is never the authorship (``hermes sessions
    open-tab --by <bot>`` stamps a chat a person started). None otherwise (also when *store* cannot answer). A read:
    the projections type that row at render time and nothing is written back."""
    from persistence.conversation.messages import TranscriptRead
    from persistence.conversation.sessions import record_side_window_stamp

    try:
        record = store.sessions.get(session_id)
        if record is None or not record_side_window_stamp(record):
            return None
        rows = store.messages.read_transcript(session_id, TranscriptRead(limit=_BRIEF_SCAN_ROWS, include_lineage=True))
        users = [row.as_wire_row() for row in rows if row.role == "user"]
        for row in users:
            if row.get("display_kind") in _MACHINE_USER_KINDS:
                continue
            if not is_bot_brief_candidate(row):
                return None
            author = _submitted_turn_author(store, row)
            return {"row_id": row["id"], "author": author} if author else None
    except Exception:  # a display hint: a store that cannot answer leaves the rows as stored
        logger.debug("bot brief lookup failed for %s", session_id, exc_info=True)
    return None


def mark_bot_brief(row: dict, brief: Optional[Mapping[str, Any]], row_id: Any) -> dict:
    """*row* (stored id *row_id*) typed ``bot_brief`` when it is *brief*'s row (``find_bot_brief``), else as is."""
    if not brief or row_id is None or row_id != brief["row_id"] or not is_bot_brief_candidate(row):
        return row
    return bot_brief_row(row, brief["author"])
