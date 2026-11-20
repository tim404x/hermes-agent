"""Silent turns: the stored rows a turn that chose silence leaves nothing to show for.

A bot may end a turn with a bare silence marker (``NO_REPLY``, ``[SILENT]``; the rule is
:func:`session_rules.silence.is_intentional_silence_response`), and the live delivery lets it
vanish only where :func:`gateway.response_filters.silence_allowed` does: a machinery wake
(background process, delegation result, runtime notification), a message nobody addressed to the
bot, or a session that is silent by nature (the Bot Chat). Every history projection hides the rows
that live turn never showed: the marker row, and the wake row ("Background Process Finished: …") of
a turn that said nothing else and ran no tool. A person's own question answered with a
marker-looking reply keeps it: the live view painted it, so a reload must too.

The stored rows are never touched. ``tui_gateway`` history reads drop the rows; the REST pages the
desktop pages by backend row offset mark them ``silent_turn`` instead. Desktop twin (for backends
older than this rule): ``apps/desktop/src/lib/chat-messages/hydration.ts::silentTurnRows``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from session_rules.silence import is_intentional_silence_response, is_machinery_display_kind

FAILED_TURN_DISPLAY_KIND = "failed_turn"


def _metadata(row: Mapping) -> dict:
    value = row.get("display_metadata")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _codex_reply(row: Mapping) -> str:
    """Visible reply text a Responses-API row keeps in ``codex_message_items`` when ``content`` is empty."""
    items = row.get("codex_message_items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return ""
    if not isinstance(items, list):
        return ""
    chunks = []
    for item in items:
        if not (isinstance(item, dict) and item.get("type") == "message" and item.get("role") == "assistant"):
            continue
        phase = str(item.get("phase") or "").strip().lower()
        if phase in ("analysis", "commentary") or not isinstance(item.get("content"), list):
            continue
        chunks.extend(part["text"] for part in item["content"]
                      if isinstance(part, dict) and part.get("type") in ("output_text", "text")
                      and isinstance(part.get("text"), str))
    return "".join(chunks)


def reply_text(row: Mapping) -> str:
    """The reply an assistant row paints, as the painters resolve it: the projected ``display_content`` when
    the projection set one, else the row's own text, else the Responses-API reply."""
    if "display_content" in row:
        return _text(row["display_content"])
    return _text(row.get("content")) or _text(row.get("text")) or _codex_reply(row)


def _public_commentary(row: Mapping) -> list[str]:
    """Public commentary the row paints beside its reply (a marker there is not commentary)."""
    commentary = row.get("display_commentary")
    if not isinstance(commentary, list):
        return []
    return [c for c in commentary if isinstance(c, str) and c.strip() and not is_intentional_silence_response(c)]


def _has_tool_calls(row: Mapping) -> bool:
    calls = row.get("tool_calls")
    return isinstance(calls, list) and bool(calls)


def _speaks(row: Mapping) -> bool:
    return bool(reply_text(row).strip() or _public_commentary(row))


def is_silent_reply_row(row: Mapping) -> bool:
    """An assistant row whose every public carrier holds nothing but a silence marker. A failed or interrupted
    turn's boundary row is Hermes reporting, never the bot choosing silence: its error card must stay."""
    if row.get("role") != "assistant" or _has_tool_calls(row) or row.get("display_kind") == FAILED_TURN_DISPLAY_KIND:
        return False
    metadata = _metadata(row)
    if metadata.get("error_surface") or metadata.get("interrupted") is True or _public_commentary(row):
        return False
    return is_intentional_silence_response(reply_text(row))


def _opens_silence_ok_turn(row: Mapping) -> bool:
    """``silence_allowed`` read from the stored opener: a machinery kind, or an unaddressed message."""
    return is_machinery_display_kind(row.get("display_kind")) or _metadata(row).get("reply_expected") is False


@dataclass(frozen=True)
class SilentTurns:
    hidden: frozenset[int] = frozenset()
    """Indexes that show nothing: silent reply rows and the wake rows of quiet silent turns."""
    silence_ok: frozenset[int] = frozenset()
    """Assistant rows inside a turn that may end silent: only there does a closing marker line drop."""


def silent_turns(rows: Sequence[Mapping[str, Any]], *, silence_ok_session: bool = False) -> SilentTurns:
    """The silent-turn projection of ``rows`` (see the module docstring).

    ``silence_ok_session``: the whole session may end turns silent (the Bot Chat). Rows before the
    first user row belong to a turn whose opener is not in ``rows`` (a page cut mid-turn); only the
    session flag can make those silent.
    """
    hidden: set[int] = set()
    silence_ok_rows: set[int] = set()
    wake: int | None = None
    silence_ok = silence_ok_session
    quiet, silent = True, False

    def close_turn() -> None:
        if wake is not None and quiet and silent:
            hidden.add(wake)

    for index, row in enumerate(rows):
        role = row.get("role")
        if role == "user":
            close_turn()
            # A cron delivery's row carries the job's output for the reader (agent/display_row_typing.py):
            # the bot's silence about it never hides the delivery itself.
            wake = index if is_machinery_display_kind(row.get("display_kind")) and not _metadata(row).get(
                "cron_delivery") else None
            silence_ok = silence_ok_session or _opens_silence_ok_turn(row)
            quiet, silent = True, False
            continue
        if silence_ok and role == "assistant":
            silence_ok_rows.add(index)
        if silence_ok and is_silent_reply_row(row):
            hidden.add(index)
            silent = True
        elif role != "assistant" or _has_tool_calls(row) or _speaks(row):
            quiet = False
    close_turn()
    return SilentTurns(frozenset(hidden), frozenset(silence_ok_rows))
