"""Read-side display typing for machine rows the transcript stores as plain rows.

Both history projections (``tui_gateway/session_history.py::_history_to_messages`` for ``session.resume`` and
``hermes_cli/web_routers/sessions.py::_project_for_display`` for the REST page the desktop prefetches) run every
stored row through :func:`type_display_row`, so rows written before their writer typed them render the way new
rows do. Render time only: nothing here writes the database.

- a background-process completion (``process_complete``) shows a job title, never its shell command
  (:func:`tools.process_registry_notifications.relabel_legacy_process_title`);
- a cron output delivered into a Bot Chat (``[Cronjob "<name>" output ...]`` frame on a ``user`` row) carries
  ``display_metadata.cron_delivery = {"job_name"}`` and its output as the display text, so it is never painted as
  the user's own message;
- an assistant row that is only an interrupt sentinel (``Operation interrupted: waiting for model response (0.3s
  elapsed).``) keeps its row and its ``interrupted`` mark with no prose.

:func:`mark_bot_brief` types the seed prompt a bot wrote into a side window it opened for itself (``bot_brief``).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

PROCESS_COMPLETE_DISPLAY_KIND = "process_complete"
BOT_BRIEF_DISPLAY_KIND = "bot_brief"
_CRON_FRAME_KINDS = (None, "", "internal_notification")

# Interrupt text the agent closes a stopped turn with (agent/conversation_loop.py
# INTERRUPT_WAITING_FOR_MODEL_PREFIX and the ``interrupt_text`` of the retry/recovery paths): cancellation
# metadata, never assistant prose. One line, nothing else in the row.
_INTERRUPT_SENTINEL_RE = re.compile(r"^Operation interrupted(?::| during )[^\n]*$")


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
        frame = parse_bot_chat_frame(_text(row.get("content")))
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
    try:
        from hermes_cli.profiles import get_profile_dir
        from tools.bot_mode_probe import _friendly_names
        names = _friendly_names(get_profile_dir(profile))
    except Exception:
        return ""
    name = next((n for n in names if n), "")
    return name.split(" | ", 1)[0].strip()


def is_bot_brief_candidate(row: Mapping[str, Any]) -> bool:
    """Whether *row* can be a bot's self-seeded brief: an untyped user row that is not an attributed DM."""
    return (row.get("role") == "user" and not row.get("display_kind")
            and not _text(row.get("content")).lstrip().startswith("Message from "))


def bot_brief_row(row: Mapping[str, Any], author: Mapping[str, Any]) -> dict:
    """*row* typed as the brief *author* (``{"id", "name"}``) seeded its side window with."""
    meta = row.get("display_metadata") if isinstance(row.get("display_metadata"), Mapping) else {}
    return {**row, "display_kind": BOT_BRIEF_DISPLAY_KIND,
            "display_metadata": {**meta, "author": {"id": author["id"], "name": author["name"]}}}
