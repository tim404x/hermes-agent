"""Regression tests for the machinery-turn empty-reply quiet exit (#139299).

Empty responses on machinery turns (``internal_notification``, ``process_complete``,
``async_delegation_complete`` display kind) end the turn quietly: no nudge, no
paid retries, no provider switch, no ``(empty)`` sentinel; the turn is closed with
the ``[SILENT]`` row so the next human prompt is not merged into the notice.
Ordinary turns and machinery turns with unfinished work keep the full ladder.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent import turn_empty_response as ter


def _assistant_msg():
    return SimpleNamespace(reasoning=None, reasoning_content=None, reasoning_details=None)


def _empty_response():
    choice = SimpleNamespace(message=_assistant_msg(), finish_reason="stop")
    choice.message.content = ""
    choice.message.tool_calls = None
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _text_response(text):
    response = _empty_response()
    response.choices[0].message.content = text
    return response


@pytest.fixture
def loop_agent(monkeypatch):
    from run_agent import AIAgent

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = lambda **_kwargs: _empty_response()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    monkeypatch.setattr("model_tools.handle_function_call", MagicMock(return_value="unused"))
    monkeypatch.setattr("agent.retry_utils.jittered_backoff", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("agent.turn_empty_response.interruptible_backoff_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(agent, "_persist_session", MagicMock())
    monkeypatch.setattr(agent, "_save_trajectory", MagicMock())
    monkeypatch.setattr(agent, "_cleanup_task_resources", MagicMock())
    return agent


def _completed_tool_round():
    return [
        {"role": "user", "content": "Run the background task."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "Process exited successfully.", "tool_call_id": "call_1"},
    ]


class TestNotificationTurnLoop:
    @pytest.mark.parametrize(
        ("display_kind", "prompt"),
        [
            ("process_complete", "[process completed] Background task finished."),
            ("internal_notification", "[kanban] Board updated."),
        ],
    )
    def test_machinery_empty_reply_exits_without_nudge(self, loop_agent, display_kind, prompt):
        result = loop_agent.run_conversation(
            prompt,
            conversation_history=_completed_tool_round(),
            persist_user_display_kind=display_kind,
        )

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["final_response"] == ""
        assert result["turn_exit_reason"] == "notification_no_reply"
        provider_messages = loop_agent.client.chat.completions.create.call_args.kwargs["messages"]
        assert all(
            "You just executed tool calls but returned an empty response."
            not in str(message.get("content") or "")
            for message in provider_messages
        )

    def test_announced_tool_call_without_payload_is_recovered_not_silenced(self, loop_agent):
        """finish_reason="tool_calls" with no tool calls is a dropped action, not "nothing to add":
        the turn keeps the recovery ladder and the model's follow-up is delivered."""
        def respond(**_kwargs):
            if loop_agent.client.chat.completions.create.call_count == 1:
                dropped = _empty_response()
                dropped.choices[0].finish_reason = "tool_calls"
                return dropped
            return _text_response("did the thing")

        loop_agent.client.chat.completions.create.side_effect = respond
        result = loop_agent.run_conversation(
            "[process completed] Background task finished.",
            conversation_history=_completed_tool_round(),
            persist_user_display_kind="process_complete",
        )

        assert loop_agent.client.chat.completions.create.call_count == 2
        assert result["final_response"] == "did the thing"
        assert result.get("turn_exit_reason") != "notification_no_reply"

    def test_human_turn_empty_reply_still_retries(self, loop_agent):
        loop_agent.run_conversation(
            "What happened?",
            conversation_history=_completed_tool_round(),
        )

        assert loop_agent.client.chat.completions.create.call_count > 1

    def test_next_human_turn_is_not_merged_into_the_quiet_notice(self, loop_agent):
        notice = "[process completed] Background task finished."
        first = loop_agent.run_conversation(
            notice,
            conversation_history=_completed_tool_round(),
            persist_user_display_kind="process_complete",
        )
        assert first["turn_exit_reason"] == "notification_no_reply"

        # The quiet exit must leave the notice answered in the transcript; otherwise the
        # pre-call repair merges the next human prompt into the notice row.
        loop_agent.client.chat.completions.create.side_effect = lambda **_kwargs: _text_response("Sure.")
        second = loop_agent.run_conversation("now do X", conversation_history=first["messages"])

        wire = loop_agent.client.chat.completions.create.call_args.kwargs["messages"]
        assert wire[-1]["role"] == "user"
        assert wire[-1]["content"] == "now do X"
        assert [m["role"] for m in wire[-3:]] == ["user", "assistant", "user"]
        assert wire[-3]["content"] == notice

        rows = [m for m in second["messages"] if m.get("role") == "user"]
        assert rows[-1]["content"] == "now do X"
        assert rows[-1].get("display_kind") is None
        assert rows[-2]["content"] == notice
        assert rows[-2].get("display_kind") == "process_complete"


def test_transcript_cannot_grant_notification_silence(loop_agent):
    rows = [{"role": "user", "content": "done", "display_kind": "process_complete"},
            {"role": "assistant", "content": "[SILENT]"}]
    loop_agent.client.chat.completions.create.side_effect = [_empty_response(), _text_response("Here it is.")]
    result = loop_agent.run_conversation("What happened?", conversation_history=rows)
    assert result["final_response"] == "Here it is."
    assert loop_agent.client.chat.completions.create.call_count == 2


class TestNotificationQuietExit:
    @pytest.mark.parametrize(
        "display_kind", ["internal_notification", "process_complete", "async_delegation_complete"]
    )
    def test_quiet_exit_closes_the_turn_with_the_silence_row(self, loop_agent, display_kind):
        result = loop_agent.run_conversation("[notice]", persist_user_display_kind=display_kind)
        messages = result["messages"]
        assert result["final_response"] == ""
        assert result["turn_exit_reason"] == ter.NOTIFICATION_NO_REPLY_EXIT
        # One closing assistant row, the same marker a deliberate no-reply leaves on a human
        # turn; never the "(empty)" failure sentinel.
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert messages[-1]["content"] == ter.MACHINERY_NO_REPLY_ROW == "[SILENT]"
        assert not messages[-1].get("_empty_terminal_sentinel")
        assert not messages[-1].get("_empty_recovery_synthetic")

    @pytest.mark.parametrize("reasoning", ["inline", "structured"])
    def test_reasoning_only_is_not_a_finished_blank(self, loop_agent, reasoning):
        private = "unfinished private thought"
        response = _empty_response()
        if reasoning == "inline":
            response.choices[0].message.content = f"<think>{private}</think>"
        else:
            response.choices[0].message.reasoning_content = private
        loop_agent.client.chat.completions.create.side_effect = [response, _text_response("Still working.")]
        result = loop_agent.run_conversation("[notice]", persist_user_display_kind="async_delegation_complete")
        assert result["final_response"] == "Still working."
        assert all(private not in (row.get("content") or "") for row in result["messages"])

    def test_blank_does_not_replace_a_visible_streamed_update(self, loop_agent):
        def respond(**_kwargs):
            loop_agent._current_streamed_assistant_text = "<think>private</think>The job is still running."
            return _empty_response()

        loop_agent.client.chat.completions.create.side_effect = respond
        result = loop_agent.run_conversation("[notice]", persist_user_display_kind="process_complete")
        assert result["turn_exit_reason"] == "partial_stream_recovery"
        assert "The job is still running." in result["final_response"]
        assert "private" not in result["final_response"]


class TestResponseFiltersNotificationNoReply:
    def test_notification_no_reply_with_empty_text_is_intentional_silence(self):
        from gateway.response_filters import is_intentional_silence_agent_result

        agent_result = {"failed": False, "turn_exit_reason": ter.NOTIFICATION_NO_REPLY_EXIT}
        assert is_intentional_silence_agent_result(agent_result, "") is True
        assert is_intentional_silence_agent_result(agent_result, " ") is True

    def test_notification_no_reply_with_non_empty_text_is_not_silence(self):
        from gateway.response_filters import is_intentional_silence_agent_result

        agent_result = {"failed": False, "turn_exit_reason": ter.NOTIFICATION_NO_REPLY_EXIT}
        assert is_intentional_silence_agent_result(agent_result, "got it") is False

    def test_notification_no_reply_failed_turn_is_not_silence(self):
        from gateway.response_filters import is_intentional_silence_agent_result

        agent_result = {"failed": True, "turn_exit_reason": ter.NOTIFICATION_NO_REPLY_EXIT}
        assert is_intentional_silence_agent_result(agent_result, "") is False

    def test_ordinary_empty_response_exhausted_is_not_notification_silence(self):
        """An empty_response_exhausted turn with no text and no silence
        marker is not intentional silence -- it falls through to normal
        delivery."""
        from gateway.response_filters import is_intentional_silence_agent_result

        agent_result = {"failed": False, "turn_exit_reason": "empty_response_exhausted"}
        # "(empty)" is not a LIVE_GATEWAY_SILENT_MARKERS entry; it reaches
        # delivery through the empty-response path, not silence.
        assert is_intentional_silence_agent_result(agent_result, "(empty)") is False
        # But with the [SILENT] marker it IS silence (unrelated to notification path).
        assert is_intentional_silence_agent_result(agent_result, "[SILENT]") is True

    def test_notification_no_reply_alongside_real_silence_marker(self):
        from gateway.response_filters import is_intentional_silence_agent_result

        agent_result = {"failed": False, "turn_exit_reason": ter.NOTIFICATION_NO_REPLY_EXIT}
        assert is_intentional_silence_agent_result(agent_result, "[SILENT]") is True

    def test_notification_no_reply_none_result_is_not_silence(self):
        from gateway.response_filters import is_intentional_silence_agent_result

        assert is_intentional_silence_agent_result(None, "") is False
