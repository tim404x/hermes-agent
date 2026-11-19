"""The one rule for turning a stored message into the short text a list row shows.

Session previews, roster "last message" lines, timeline entries and derived titles all read raw ``messages.content``,
and that cell holds model-facing frames as well as the user's words: a mid-turn steer is stored wrapped in the
``[OUT-OF-BAND USER MESSAGE …]`` marker, background-process / delegation completions arrive as ``[IMPORTANT: …]`` /
``[ASYNC DELEGATION …]`` user rows, stop gates add ``[System: …]`` nudges, and runtime/system notes ride on real
prompts. The chat view projects those away; every list surface must too, or a sidebar row reads
``[OUT-OF-BAND USER MES…`` instead of what the user typed.

:func:`display_preview_text` is that projection for any text snippet:

- a steer is unwrapped to the user's words (also when the snippet was cut before the closing marker);
- a slash-skill scaffold renders as the typed ``/skill — request``;
- a compaction carrier keeps only the user message after the summary's end marker;
- ``[Runtime note: …]`` / ``[System note: …]`` blocks and ``<memory-context>`` fences are removed from real text;
- a row that is nothing but a harness frame (``[IMPORTANT: …]``, ``[System: …]``, ``[ASYNC DELEGATION …]``, a pure
  compaction summary, …) yields ``""`` so the caller can fall back to an older row.

The desktop keeps a byte-for-byte twin in ``apps/desktop/src/lib/preview-text.ts`` (for previews cached or served by an
older backend); both are pinned by the shared cases in ``tests/fixtures/preview_text_cases.json``.
"""

from __future__ import annotations

import re
from typing import Any

from session_rules.turns import (
    LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX, _MERGED_PRIOR_CONTEXT_HEADER, _SUMMARY_END_MARKER, describe_skill_invocation,
    sanitize_context,
)

# Both halves of the steer frame (agent.prompt_builder STEER_MARKER_OPEN / STEER_MARKER_CLOSE). The opener's wording
# may evolve, so match its stable head up to its closing bracket, or to the end of the line when a snippet was cut
# inside it. The opener carries no inner "]".
_STEER_FRAME_RE = re.compile(r"\[/?OUT-OF-BAND USER MESSAGE[^\]\n]*(?:\]|$)", re.MULTILINE)

# Notes that ride on a real prompt: removed, the rest kept. A terminated block ends at a "]" closing its line; an
# unterminated one (cut snippet) runs to the end of the text.
_INLINE_NOTE_RE = re.compile(r"\[(?:Runtime note|System note):[\s\S]*?\](?=[ \t]*(?:\n|$))")
_INLINE_NOTE_TAIL_RE = re.compile(r"\[(?:Runtime note|System note):[\s\S]*$")

# Openers of rows that are harness frames from end to end: such a row has no user words to show. (A slash-skill
# scaffold also opens with "[IMPORTANT:"; it is described as the typed command before this check runs.)
HARNESS_FRAME_OPENERS = (
    "[IMPORTANT:", "[ASYNC DELEGATION", "[System:", "[SYSTEM]", "[Planning state preserved", "[Your active task list",
)
# A compaction carrier is matched by its whole introduction, not the bracketed label: a user asking what
# "[CONTEXT COMPACTION — REFERENCE ONLY]" means wrote a real message.
COMPACTION_FRAME_OPENERS = (
    " ".join(SUMMARY_PREFIX.split("Do NOT answer", 1)[0].split()), LEGACY_SUMMARY_PREFIX, _MERGED_PRIOR_CONTEXT_HEADER,
)
WHOLE_ROW_FRAME_OPENERS = HARNESS_FRAME_OPENERS + COMPACTION_FRAME_OPENERS


def display_preview_text(raw: Any) -> str:
    """*raw* as a list row shows it: harness frames removed, whitespace collapsed; ``""`` when nothing is left."""
    text = str(raw or "")
    if not text.strip():
        return ""
    described = describe_skill_invocation(text.lstrip())
    if described is not None:
        text = described
    if _SUMMARY_END_MARKER in text:
        text = text.rsplit(_SUMMARY_END_MARKER, 1)[1]
    text = _STEER_FRAME_RE.sub(" ", text)
    text = sanitize_context(text)
    text = _INLINE_NOTE_TAIL_RE.sub(" ", _INLINE_NOTE_RE.sub(" ", text))
    text = " ".join(text.split())
    if text.startswith(WHOLE_ROW_FRAME_OPENERS):
        return ""
    return text
