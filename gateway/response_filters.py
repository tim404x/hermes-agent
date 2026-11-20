"""Gateway response filtering helpers.

These decide whether a completed agent turn should be delivered to the chat,
not what should be persisted in conversation history. The silence-marker rule
itself (marker recognition, the machinery turn kinds) lives in
:mod:`session_rules.silence`, shared with storage and the history projections.
"""

from __future__ import annotations

from typing import Any, Optional

from session_rules.silence import (
    INTERNAL_NOTIFICATION_DISPLAY_KIND,
    LIVE_GATEWAY_SILENT_MARKERS,
    MACHINERY_DISPLAY_KINDS,
    PRODUCER_NOTICE_DISPLAY_KINDS,
    SilenceMarkerStreamHold,
    ends_with_partial_silence_marker,
    is_autonomous_silence_response,
    is_intentional_silence_response,
    is_machinery_display_kind,
    is_partial_silence_marker,
    strip_trailing_silence_marker,
)

__all__ = [
    "INTERNAL_NOTIFICATION_DISPLAY_KIND", "LIVE_GATEWAY_SILENT_MARKERS", "MACHINERY_DISPLAY_KINDS",
    "NOTIFICATION_NO_REPLY_EXIT", "PRODUCER_CARD_KINDS", "PRODUCER_NOTICE_DISPLAY_KINDS", "SilenceMarkerStreamHold",
    "display_kind_for_event", "display_metadata_for_event", "ends_with_partial_silence_marker",
    "is_autonomous_silence_response", "is_intentional_silence_agent_result", "is_intentional_silence_response",
    "is_machinery_display_kind", "is_partial_silence_marker", "persisted_user_metadata_for_event",
    "reply_expected_metadata", "silence_allowed", "strip_trailing_silence_marker",
]

# Loop exit the agent stamps when a machinery turn ends with the model adding nothing
# (agent/turn_empty_response.py NOTIFICATION_NO_REPLY_EXIT mirrors this; agent/ cannot import gateway/).
NOTIFICATION_NO_REPLY_EXIT = "notification_no_reply"
# What a gateway producer's card may name (a liveness tick names internal_notification for its CLI
# receipt). Closed, and kept apart from MACHINERY_DISPLAY_KINDS so a kind added there never becomes
# nameable; never a client-chosen presentation such as ``hidden``.
PRODUCER_CARD_KINDS = frozenset({INTERNAL_NOTIFICATION_DISPLAY_KIND, *PRODUCER_NOTICE_DISPLAY_KINDS, "plugin_card"})


def is_intentional_silence_agent_result(agent_result: dict | None, response: Any) -> bool:
    """Silence markers suppress delivery only for successful agent turns.

    The loop's quiet exit on a runtime-notification turn (``notification_no_reply``:
    the model was handed a completion/delegation notice and added nothing) counts as
    intentional silence too, and only ever with no text to deliver;
    ``silence_allowed`` still gates it by turn kind at the delivery seam, so a human
    turn that somehow carried the reason gets a visible fallback instead of a drop.
    """
    if not (isinstance(agent_result, dict) and not agent_result.get("failed")):
        return False
    if is_intentional_silence_response(response):
        return True
    return agent_result.get("turn_exit_reason") == NOTIFICATION_NO_REPLY_EXIT and not (
        isinstance(response, str) and response.strip()
    )


def display_kind_for_event(event: Any) -> str | None:
    """The persisted user-row kind for a gateway turn: only self-injected events are machinery.

    A scheduled heartbeat prompt is self-injected too (``_heartbeat_session_id`` is stamped only
    by the gateway poller, never inferred from inbound text), but it deliberately stays
    non-internal so authorization and the emergency stop still apply to it.
    """
    from gateway.session_display import admission_display
    if kind := admission_display(event).get("kind"):
        # A canonical admission committed hidden (Desktop widget intent): never a user bubble.
        return kind
    if getattr(event, "internal", False) and _producer_card(event):
        return event.metadata["display_kind"]
    if getattr(event, "internal", False) or getattr(event, "_heartbeat_session_id", None):
        return INTERNAL_NOTIFICATION_DISPLAY_KIND
    return None


def _producer_card(event: Any) -> dict:
    metadata = getattr(event, "metadata", None) or {}
    text = metadata.get("display_text")
    if (getattr(event, "internal", False) and metadata.get("display_kind") in PRODUCER_NOTICE_DISPLAY_KINDS
            and isinstance(text, str) and text.strip()):
        return {"display_text": text}
    return {}


def display_metadata_for_event(event: Any) -> dict:
    """``{"display_text": ...}`` for an internal event whose producer named an async-result card, plus the
    ``title_preview`` (titler-only) a canonical admission committed for this exact event."""
    from gateway.session_display import admission_display
    preview = admission_display(event).get("title_preview")
    return {**_producer_card(event), **({"title_preview": preview} if preview else {})}


def persisted_user_metadata_for_event(event: Any) -> dict:
    """The user row's display metadata: ``display_metadata_for_event`` plus the canonical admission's
    ``submission_id``, which lets a client match a send whose ack it never received to its stored row."""
    from gateway.session_display import admission_submission
    return {**display_metadata_for_event(event), **admission_submission(event)}


def silence_allowed(
    display_kind: Any, reply_expected: Optional[bool] = None, *,
    allow_silence: Optional[bool] = None, agent_result: Optional[dict] = None,
) -> bool:
    """Keep existing exemptions; room opt-in additionally requires a healthy completed result.

    Marker recognition stays with the caller. Extra health restrictions apply only to the new
    room permission, preserving the established internal/unaddressed semantics.
    """
    if is_machinery_display_kind(display_kind) or reply_expected is False:
        return True
    return (allow_silence is True and isinstance(agent_result, dict)
            and not any(agent_result.get(k) for k in ("failed", "partial", "interrupted", "error"))
            and agent_result.get("completed") is not False)


def reply_expected_metadata(reply_expected: Optional[bool]) -> dict:
    """The persisted user row's ``reply_expected`` key, only when the adapter knew; crash recovery
    reads it back to judge a silence marker as the live turn did."""
    return {} if reply_expected is None else {"reply_expected": reply_expected}
