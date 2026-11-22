"""Tests for the auto-continue feature (#4493 / #45232).

When the gateway restarts mid-agent-work, the session transcript can end on a
tool result that the agent never processed.  The auto-continue logic detects
this and prepends an API-only system note to the next user message so the model
does not re-execute stale interrupted tool calls before addressing new input.
"""


def _simulate_auto_continue(agent_history: list, user_message: str) -> str:
    """Reproduce the auto-continue injection logic from _run_agent().

    This mirrors the exact code in gateway/run.py so we can test the
    detection and message transformation without spinning up a full
    gateway runner.
    """
    message = user_message
    if agent_history and agent_history[-1].get("role") == "tool":
        message = (
            "[System note: A new message has arrived. The conversation "
            "history contains pending tool outputs from an interrupted turn. "
            "IGNORE those pending results. Address the user's NEW message "
            "below FIRST. Do NOT re-execute old tool calls from the history.]\n\n"
            + message
        )
    return message




class TestInterruptedReplayFiltering:
    def test_interrupted_side_effect_is_replayed_as_unknown(self):
        from gateway.run import _build_gateway_agent_history

        history = [
            {"role": "user", "content": "transcribe this video"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}},
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"exit_code": 130, "output": "[Command interrupted]"}',
            },
        ]

        agent_history, observed_context = _build_gateway_agent_history(history)

        assert observed_context is None
        assert agent_history[:2] == history[:2]
        assert agent_history[-1]["role"] == "tool"
        assert agent_history[-1]["tool_call_id"] == "call_1"
        assert agent_history[-1]["effect_disposition"] == "unknown"


    def test_dangling_unanswered_side_effect_is_replayed_as_unknown(self):
        """A trailing side-effecting call gets an UNKNOWN result, not a retry.

        This is the SIGKILL signature from #49201: the tool itself ran a
        restart/shutdown command and killed the gateway before its result was
        persisted. The transcript tail is an assistant message with tool_calls
        and zero matching tool rows. A synthetic UNKNOWN result closes the tool
        pair without claiming the restart did not happen or inviting a retry.
        """
        from gateway.run import _build_gateway_agent_history

        history = [
            {"role": "user", "content": "restart the container"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command": "docker restart hermes-agent"}',
                        },
                    },
                ],
            },
        ]

        agent_history, _observed_context = _build_gateway_agent_history(history)

        assert agent_history[:2] == history
        assert agent_history[-1]["role"] == "tool"
        assert agent_history[-1]["tool_call_id"] == "call_1"
        assert agent_history[-1]["effect_disposition"] == "unknown"




# ---------------------------------------------------------------------------
# The notes are API-only: the stored user row holds the person's words only.
# A Telegram image turn after an interrupted turn stored "[System note: ...]\n\n<question>
# [Image attached ...]\n[System: This conversation is now being answered on a different
# interface ...]" and the desktop painted the note as the user's bubble.
# ---------------------------------------------------------------------------

from types import SimpleNamespace as _NS

_PENDING_NOTE_PREFIX = "[System note: A new message has arrived."
_SURFACE_NOTE = ("[System: This conversation is now being answered on a different interface: telegram"
                 " \u2014 any earlier interface guidance in this conversation is superseded \u2014 follow"
                 " the interface section in the system prompt above for formatting, file delivery and any"
                 " interface-specific capability.]")


def _turn_runner(message, *, persist=None, model_note=None):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    ctx = TurnContext(
        message=message, session_id="s-1", session_key="telegram:dm:1",
        source=_NS(user_id="u-1", user_name="Tim", is_bot=False),
        history=[
            {"role": "user", "content": "earlier ask"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        ],
    )
    ctx.persist_user_message = persist
    runner = object.__new__(TurnRunner)
    runner._ctx = ctx
    runner._runner = _NS(
        session_store=_NS(_entries={}),
        _pending_model_notes={ctx.session_key: model_note} if model_note else {},
        _pending_skills_reload_notes={},
    )
    return runner, ctx


class TestAutoContinueNoteIsApiOnly:
    def _prepare(self, runner):
        return runner._prepare_turn_message(list(runner._ctx.history))

    def test_pending_tools_note_reaches_model_not_stored_row(self):
        runner, ctx = _turn_runner("why the sessions arent the same?")
        persist, _ts = self._prepare(runner)
        assert ctx.message.startswith(_PENDING_NOTE_PREFIX)
        assert ctx.message.endswith("why the sessions arent the same?")
        assert persist == "why the sessions arent the same?"

    def test_model_switch_note_is_not_folded_into_the_stored_row(self):
        note = "[Note: model was just switched from a to b via x. Adjust your self-identification accordingly.]"
        runner, ctx = _turn_runner("next question", model_note=note)
        persist, _ts = self._prepare(runner)
        assert note in ctx.message and _PENDING_NOTE_PREFIX in ctx.message
        assert persist == "next question"

    def test_existing_clean_override_survives_the_note(self):
        # The timestamp-strip path hands a clean override; the recovery branch must not replace it
        # with the model-facing (timestamped) text.
        runner, _ctx = _turn_runner("[Sat 2026-11-21 17:14] hello", persist="hello")
        persist, _ts = self._prepare(runner)
        assert persist == "hello"

    def test_native_image_turn_persists_clean_list(self, monkeypatch):
        runner, ctx = _turn_runner("why the sessions arent the same?")
        persist, _ts = self._prepare(runner)
        hints = "\n\n[Image attached at: /cache/img/shot.jpg]"
        image_part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
        live = [{"type": "text", "text": ctx.message.strip() + hints}, image_part]
        monkeypatch.setattr(runner, "_native_image_run_message", lambda: live)
        captured = {}

        class Agent:
            def run_conversation(self, message, **kwargs):
                captured["message"] = message
                captured["persist"] = kwargs.get("persist_user_message")
                return {"final_response": "done"}

        runner._run_conversation_with_approval(Agent(), [], None, persist, None)

        assert captured["message"][0]["text"].startswith(_PENDING_NOTE_PREFIX)
        assert captured["persist"] == [
            {"type": "text", "text": "why the sessions arent the same?" + hints}, image_part,
        ]
        # The agent appends per-turn notes to the LIVE list; the stored copy must not see them.
        assert captured["persist"] is not captured["message"]

    def test_list_row_stores_clean_text_and_keeps_sent_bytes_in_sidecar(self):
        from agent.session_persistence import durable_user_row_content

        image_part = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}
        clean = [{"type": "text", "text": "why?\n\n[Image attached at: /cache/img/shot.jpg]"}, image_part]
        live = [{"type": "text", "text": "[System note: A new message has arrived.]\n\n" + clean[0]["text"]},
                image_part, {"type": "text", "text": _SURFACE_NOTE}]
        agent = _NS(_persist_user_message_override=clean)

        content, api_content = durable_user_row_content(agent, {"role": "user", "content": live}, live, None)

        assert content == clean
        assert api_content.startswith("[System note: A new message has arrived.]")
        assert _SURFACE_NOTE in api_content  # replay parity + surface switch read-back

    def test_list_row_without_notes_writes_no_sidecar(self):
        from agent.session_persistence import durable_user_row_content

        clean = [{"type": "text", "text": "why?"}, {"type": "image_url", "image_url": {"url": "data:,"}}]
        agent = _NS(_persist_user_message_override=list(clean))
        content, api_content = durable_user_row_content(agent, {"role": "user", "content": clean}, clean, None)
        assert content == clean and api_content is None
