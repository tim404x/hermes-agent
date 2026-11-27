"""A Telegram wake runs its tools and settles through the same delivery seam as human input."""

from unittest.mock import MagicMock

import pytest

from gateway.run_notifications import _mark_internal_notification
from gateway.run_turn_runner import TurnRunner
from tests.agent import test_empty_response_recovery_persistence as recovery
from tests.fixtures.storage_read import conversation
from tests.gateway.test_gateway_silence_tokens import _event, _runner
from tests.gateway.test_run_progress_topics import ProgressCaptureAdapter

real_loop = recovery.real_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["internal", "marker", "stream_marker", "human", "steer", "steer_marker", "steer_think_marker",
                                  "steer_stream_marker", "addressed", "update", "tool_error", "length", "auth"])
async def test_async_notice_settlement(case, real_loop, tmp_path, monkeypatch):
    runner = _runner(monkeypatch, tmp_path)
    agent = real_loop.agent
    runner._session_db = real_loop.db
    runner.session_store._entries = {}
    entry = runner.session_store.get_or_create_session.return_value
    entry.session_id = real_loop.sid
    monkeypatch.setattr(runner, "_resolve_session_agent_runtime", lambda **_: (agent.model, {"provider": agent.provider}))
    monkeypatch.setattr(TurnRunner, "_resolve_turn_agent", lambda *_: (agent, False))
    monkeypatch.setattr("agent.turn_empty_response.interruptible_backoff_sleep", lambda *args, **kwargs: None)
    event = _event(internal=case != "human")
    quiet = case in {"internal", "marker", "stream_marker"}
    if quiet:
        adapter = ProgressCaptureAdapter()
        runner.adapters[event.source.platform] = adapter
    if case == "addressed":
        event.reply_expected = True
    event.text = _mark_internal_notification("[ASYNC DELEGATION BATCH COMPLETE] Record the result.")
    event.metadata = {"display_kind": "internal_notification"}
    write = recovery._write_file_call(real_loop.ledger)
    if case == "tool_error":
        write = recovery._call("read_file", {"path": str(tmp_path / "missing")}, "call_read")
    calls = []

    def respond(**request):
        calls.append(request)
        if len(calls) == 1:
            if case.startswith("steer"):
                assert agent.steer("Tell me the outcome.")
            return recovery._response(finish_reason="tool_calls", tool_calls=[write])
        if case == "auth":
            import httpx
            from openai import AuthenticationError

            raise AuthenticationError("invalid API key", response=httpx.Response(
                401, request=httpx.Request("POST", "http://localhost/completions")), body=None)
        if len(calls) == 2 and case in {"marker", "steer_marker"}:
            return recovery._response("[SILENT]")
        if len(calls) == 2 and case == "steer_think_marker":
            return recovery._response("<think>done</think>[SILENT]")
        if len(calls) == 2 and case in {"stream_marker", "steer_stream_marker"}:
            agent._current_streamed_assistant_text = "<think>done</think>[SILENT]"
            return recovery._response()
        if len(calls) == 2 and case == "length":
            return recovery._response(finish_reason="length")
        if case == "update" or (len(calls) > 2 and case != "internal"):
            return recovery._response("The file was missing." if case == "tool_error" else "Here is the outcome.")
        return recovery._response()

    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = respond
    delivered = await runner._handle_message_with_agent(event, event.source, entry.session_key, 1)
    if case == "auth":
        assert delivered and any(word in delivered.lower() for word in ("auth", "key", "401"))
    else:
        assert len(calls) == (2 if quiet or case == "update" else 3), delivered
        assert delivered == ("" if quiet else "The file was missing." if case == "tool_error" else "Here is the outcome.")
    rows = conversation(real_loop.db, real_loop.sid)
    tools = [row for row in rows if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tools] == [write.id]
    if case != "tool_error":
        assert real_loop.ledger.read_text() == "PAYMENT #1 SENT\n"
    if quiet:
        assert rows[-1]["content"] == "[SILENT]"
        updates = [message["content"].lower() for message in (*adapter.sent, *adapter.edits)]
        assert not any(word in update for update in updates for word in (
            "didn't produce a reply", "empty", "provider", "retr", "⚠",
        )), updates


@pytest.mark.asyncio
async def test_live_incident_unconsumed_steer_on_reasoning_notice_hands_off_quietly(real_loop, tmp_path, monkeypatch):
    """Exact replay of the 27 Nov 2026 Telegram incident:
    an internal_notification wakes the session, a human message is steered mid-call,
    and the thinking model produces reasoning-only blanks (no tools). The background wake
    must settle quietly without emitting 'No reply' or cost banners, and the steered human
    message must immediately run as the next turn and deliver its reply."""
    runner = _runner(monkeypatch, tmp_path)
    agent = real_loop.agent
    runner._session_db = real_loop.db
    runner.session_store._entries = {}
    entry = runner.session_store.get_or_create_session.return_value
    entry.session_id = real_loop.sid
    monkeypatch.setattr(runner, "_resolve_session_agent_runtime", lambda **_: (agent.model, {"provider": agent.provider}))
    monkeypatch.setattr(TurnRunner, "_resolve_turn_agent", lambda *_: (agent, False))
    monkeypatch.setattr("agent.turn_empty_response.interruptible_backoff_sleep", lambda *args, **kwargs: None)
    event = _event(internal=True)
    adapter = ProgressCaptureAdapter()
    runner.adapters[event.source.platform] = adapter
    event.text = _mark_internal_notification("[ASYNC DELEGATION BATCH COMPLETE — deleg_6] Task 6/9 finished.")
    event.metadata = {"display_kind": "internal_notification"}

    calls = []

    def respond(**request):
        calls.append(request)
        if len(calls) == 1:
            assert agent.steer("Can you create the full plan?")
            resp = recovery._response()
            resp.choices[0].message.reasoning_content = "waiting on remaining sibling tasks"
            return resp
        if len(calls) == 2:
            resp = recovery._response()
            resp.choices[0].message.reasoning_content = "still nothing to add"
            return resp
        return recovery._response("Full plan saved and sent for adversarial review.")

    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = respond
    delivered = await runner._handle_message_with_agent(event, event.source, entry.session_key, 1)
    assert len(calls) == 3
    assert delivered == "Full plan saved and sent for adversarial review."
    handoff_messages = calls[2]["messages"]
    assert handoff_messages[-1]["role"] == "user"
    assert "Can you create the full plan?" in str(handoff_messages[-1]["content"])
    assert handoff_messages[-2]["role"] == "assistant"
    assert handoff_messages[-2]["content"] == "[SILENT]"
    updates = [message["content"].lower() for message in (*adapter.sent, *adapter.edits)]
    assert not any(word in update for update in updates for word in (
        "didn't produce a reply", "estimated cost", "send `continue`", "⚠",
    )), updates
