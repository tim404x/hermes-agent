"""Lean transcript pages for clients that render references themselves (the Desktop).

A REST transcript page carries every row verbatim, so a chat Tim pasted screenshots into
re-ships each screenshot's base64 bytes on every page read (a Bot Chat switch: 615 KB gzip for
one 439 KB PNG; base64 PNG does not compress). Two opt-in projections, display-only — the
stored rows the agent replays are never touched:

``image_refs``
    A user row persists a pasted image twice: as an ``@image:<path>`` (or
    ``[Image attached at: <path>]``) line in its text part AND as a ``data:`` image part
    (``tui_gateway/session_history.py::_build_persist_user_message``). A client that renders
    the reference loads the file through the authenticated media route on demand, so the inline
    copy is pure weight (and the Desktop painted it as a second thumbnail). Drop the data parts
    only when the row's references cover every one of them and each referenced file still
    exists; anything else keeps its bytes, so no image is ever lost from the page.

``replay_fields=False``
    Provider replay blobs (thinking ``signature``, redacted-thinking ``data``, ``projection``,
    Responses ``encrypted_content``) are opaque, incompressible, and only matter when a turn is
    replayed to the provider from the stored row. The Desktop already skips them
    (``lib/chat-messages/parts.ts``); dropping them saved ~100 KB gzip on the same page. The
    readable reasoning text next to them stays.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_IMAGE_PART_KINDS = frozenset({"image_url", "input_image", "image"})
_TEXT_PART_KINDS = frozenset({"text", "input_text"})
# Whole-line references, the forms the persist path and native ingress write.
_IMAGE_REF_LINE_RE = re.compile(r"^@image:(`[^`\n]+`|\"[^\"\n]+\"|'[^'\n]+'|\S+)[ \t]*$", re.MULTILINE)
_IMAGE_HINT_RE = re.compile(r"\[Image attached(?: at)?: ([^\n\]]+)\]")
_REPLAY_KEYS = frozenset({"signature", "data", "projection", "encrypted_content"})


def _part_data_url(part: Any) -> bool:
    """True for an image part whose payload is an inline ``data:`` URL."""
    if not isinstance(part, dict) or part.get("type") not in _IMAGE_PART_KINDS:
        return False
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if url is None:
        url = part.get("url")
    return isinstance(url, str) and url[:5].lower() == "data:"


def _referenced_image_paths(text: str) -> list[str]:
    refs = [m.group(1) for m in _IMAGE_REF_LINE_RE.finditer(text)]
    refs = [r[1:-1] if len(r) > 1 and r[0] == r[-1] and r[0] in "`\"'" else r for r in refs]
    return refs + [m.group(1).strip() for m in _IMAGE_HINT_RE.finditer(text)]


def _is_servable_file(path: str) -> bool:
    try:
        return Path(path).expanduser().is_file()
    except (OSError, ValueError):
        return False


def _without_referenced_images(message: dict) -> dict:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return message
    inline = [part for part in content if _part_data_url(part)]
    if not inline:
        return message
    text = "\n".join(str(part.get("text") or "") for part in content
                     if isinstance(part, dict) and part.get("type") in _TEXT_PART_KINDS)
    refs = _referenced_image_paths(text)
    # Every inline copy must have its own reference, and every reference must still load.
    if len(refs) < len(inline) or not all(_is_servable_file(ref) for ref in refs):
        return message
    return {**message, "content": [part for part in content if not _part_data_url(part)]}


def _strip_replay(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_replay(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_replay(item) for key, item in value.items() if key not in _REPLAY_KEYS}
    return value


def _stripped_replay_column(value: Any) -> Any:
    """Strip one replay column, keeping its wire type: the store ships these as JSON TEXT."""
    if isinstance(value, (list, dict)):
        return _strip_replay(value)
    if not isinstance(value, str) or not value.lstrip()[:1] in ("[", "{"):
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return value
    return json.dumps(_strip_replay(decoded), ensure_ascii=False)


def _without_replay_fields(message: dict) -> dict:
    out = message
    for key in ("reasoning_details", "codex_reasoning_items"):
        value = message.get(key)
        if value is None:
            continue
        stripped = _stripped_replay_column(value)
        if stripped is not value:
            if out is message:
                out = dict(message)
            out[key] = stripped
    return out


def lean_page_message(message: dict, *, image_refs: bool = False, replay_fields: bool = True) -> dict:
    """*message* (a wire row) with the opted-in display projections applied; unchanged otherwise."""
    if image_refs:
        message = _without_referenced_images(message)
    if not replay_fields:
        message = _without_replay_fields(message)
    return message
