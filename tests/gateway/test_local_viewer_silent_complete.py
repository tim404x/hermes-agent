"""A desktop turn on the canonical gateway that chose silence completes with ``silent: true``.

The desktop skips the completion toast, sound and unread dot for it (Tim, 19 Nov 2026: a background
process finished, the bot answered NO_REPLY, and "Hermes finished / Message complete." still popped).
Only a turn the delivery seam may leave silent is marked: a machinery wake or an unaddressed message.
"""
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_results import execution_result
from gateway.session_settlement_recovery import completion_payload
from gateway.turn_context import TurnContext
from tests.gateway.test_local_viewer_events_submit import _gateway_runner, _RecordingAgent


class _ReplyAgent(_RecordingAgent):
    reply = "NO_REPLY"

    def run_conversation(self, message, **kwargs):
        return {"final_response": type(self).reply, "messages": []}


def _settle(display_kind, reply):
    _ReplyAgent.reply = reply
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="sid", user_id="u"),
        message="[IMPORTANT: Background process proc_1 completed (exit 0).]", history=[], session_id="sid",
        session_key="key", user_config={}, AIAgent=_ReplyAgent, resolve_display_setting=lambda *_a: False,
        _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=False),
        persist_user_display_kind=display_kind)
    captured = {}
    token = execution_result.set(captured)
    try:
        turn = TurnRunner(_gateway_runner(), ctx)
        turn._approval_owner = (SimpleNamespace(sessions={"sid": SimpleNamespace(tool_progress_mode=None)},
                                                profile_id="p", adopt_agent=lambda *a: None), "sid", 1)
        turn.run_sync()
    finally:
        execution_result.reset(token)
    return completion_payload({"admission_id": "a1", "request_id": "r1"}, {"outcome": "completed"}, "", captured)


def test_a_silent_wake_completes_marked_silent():
    from tui_gateway.contracts.registry import EVENTS
    complete = _settle("process_complete", "NO_REPLY \u26a1")
    assert complete["silent"] is True
    EVENTS["message.complete"].payload.model_validate(
        {k: v for k, v in complete.items() if k not in ("content", "admission_id", "outcome")})


def test_a_real_reply_or_a_persons_marker_is_not_marked():
    assert "silent" not in _settle("process_complete", "Build failed: 3 tests.")
    assert "silent" not in _settle(None, "NO_REPLY")
