"""A quiet machinery stop requires a completed response, settled work and ordinary stop gates."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.bedrock_adapter import normalize_converse_response
from agent.conversation_loop import _EMPTY_TOOL_RESPONSE_NUDGE
from agent.tool_result_classification import GUARDRAIL_REFUSAL_KEY
from tools.registry import tool_error
from tests.agent import test_empty_response_recovery_persistence as recovery

real_loop = recovery.real_loop


def _run_notice(real_loop, monkeypatch, *responses):
    model = Mock(side_effect=responses)
    monkeypatch.setattr(real_loop.agent, "_interruptible_api_call", model)
    monkeypatch.setattr("agent.turn_empty_response.interruptible_backoff_sleep", lambda *args, **kwargs: None)
    result = real_loop.agent.run_conversation("Process the result.", persist_user_display_kind="internal_notification")
    return result, model.call_args_list


def _tools(*calls, content=""):
    return recovery._response(content, finish_reason="tool_calls", tool_calls=calls)


def test_duplicate_call_settles_after_one_execution(real_loop, monkeypatch):
    import model_tools

    dispatch = Mock(wraps=model_tools.handle_function_call)
    monkeypatch.setattr(model_tools, "handle_function_call", dispatch)
    first = recovery._write_file_call(real_loop.ledger)
    duplicate = recovery._write_file_call(real_loop.ledger)
    duplicate.id = "call_duplicate"
    result, requests = _run_notice(real_loop, monkeypatch, _tools(first, duplicate),
                                   recovery._response(), recovery._response("Unexpected retry."))
    assert real_loop.ledger.read_text() == "PAYMENT #1 SENT\n"
    dispatch.assert_called_once()
    assert [row["tool_call_id"] for row in result["messages"] if row["role"] == "tool"] == [first.id]
    assert len(requests) == 2
    assert result["messages"][-1]["content"] == "[SILENT]"


def test_dedup_can_make_remaining_ids_require_normalization(real_loop, monkeypatch):
    first = recovery._write_file_call(real_loop.ledger)
    first.id = "chatcmpl-tool-one"
    duplicate = recovery._write_file_call(real_loop.ledger)
    second_path = real_loop.ledger.with_name("second.txt")
    second = recovery._write_file_call(second_path)
    second.id = "chatcmpl-tool-two"
    result, requests = _run_notice(real_loop, monkeypatch, _tools(first, duplicate, second), recovery._response(),
                                   recovery._response("Unexpected retry."))
    calls = [call["id"] for row in result["messages"] for call in row.get("tool_calls") or []]
    receipts = [row["tool_call_id"] for row in result["messages"] if row["role"] == "tool"]
    assert len(calls) == len(set(receipts)) == 2
    assert set(calls) == set(receipts) and not {first.id, second.id}.intersection(calls)
    assert real_loop.ledger.read_text() == second_path.read_text() == "PAYMENT #1 SENT\n"
    assert len(requests) == 2 and result["messages"][-1]["content"] == "[SILENT]"


@pytest.mark.parametrize("real_loop", [["file", "terminal"]], indirect=True)
def test_rejected_foreground_terminal_call_cannot_settle_notice(real_loop, monkeypatch):
    rejected = recovery._call("terminal", {"command": "pwd", "notify": True}, "call_rejected")
    result, requests = _run_notice(real_loop, monkeypatch, _tools(rejected), recovery._response(),
                                   recovery._response("The command was rejected."))
    tool_row, = [row for row in result["messages"] if row["role"] == "tool"]
    assert "notify only applies to background commands" in tool_row["content"]
    assert len(requests) == 3
    assert result["final_response"] == "The command was rejected."


def test_neutral_guardrail_refusal_is_not_completed_work(real_loop, monkeypatch):
    refusal = tool_error("Already read; the tool did not run.", **{GUARDRAIL_REFUSAL_KEY: True})
    monkeypatch.setattr("model_tools.handle_function_call", Mock(return_value=refusal))
    read = recovery._call("read_file", {"path": str(real_loop.ledger)}, "call_read")
    result, requests = _run_notice(real_loop, monkeypatch, _tools(read), recovery._response(),
                                   recovery._response("No new work was performed."))
    assert len(requests) == 3
    assert result["final_response"] == "No new work was performed."


def test_narration_then_two_blanks_needs_fresh_work_to_settle(real_loop, monkeypatch):
    write = recovery._write_file_call(real_loop.ledger)
    read = recovery._call("read_file", {"path": str(real_loop.ledger)}, "call_read")
    result, requests = _run_notice(real_loop, monkeypatch, _tools(write, content="Next I will read the file."),
                                   recovery._response(), recovery._response(), _tools(read), recovery._response())
    assert len(requests) == 5
    assert _EMPTY_TOOL_RESPONSE_NUDGE in requests[2].args[0]["messages"][-1]["content"]
    assert not any(_EMPTY_TOOL_RESPONSE_NUDGE in (row.get("content") or "") for row in result["messages"])
    assert [row["tool_call_id"] for row in result["messages"] if row["role"] == "tool"] == [write.id, read.id]
    assert result["messages"][-1]["content"] == "[SILENT]"


@pytest.mark.parametrize("candidate", ["", "[SILENT]"], ids=["blank", "marker"])
def test_quiet_candidate_delivers_late_diagnostic_before_stopping(real_loop, monkeypatch, candidate):
    path = real_loop.ledger.with_suffix(".py")
    write = recovery._call("write_file", {"path": str(path), "content": "unbound_name\n"}, "call_write")
    monkeypatch.setattr("agent.lsp.deferred.settle_for_agent", Mock(side_effect=[[(str(path), "unbound name")], []]))
    result, requests = _run_notice(real_loop, monkeypatch, _tools(write), recovery._response(candidate),
                                   recovery._response("The diagnostic needs attention."))
    assert len(requests) == 3
    assert "unbound name" in requests[-1].args[0]["messages"][-1]["content"]
    assert result["final_response"] == "The diagnostic needs attention."


def test_completed_followup_can_settle_after_withheld_stop(real_loop, monkeypatch):
    path = real_loop.ledger.with_suffix(".py")
    bad = recovery._call("write_file", {"path": str(path), "content": "unbound_name\n"}, "call_bad")
    fix = recovery._call("write_file", {"path": str(path), "content": "value = 1\n"}, "call_fix")
    monkeypatch.setattr("agent.lsp.deferred.settle_for_agent", Mock(side_effect=[[(str(path), "unbound name")], []]))
    result, requests = _run_notice(real_loop, monkeypatch, _tools(bad), recovery._response(),
                                   recovery._response("[SILENT]"), _tools(fix), recovery._response())
    assert len(requests) == 5
    assert path.read_text() == "value = 1\n"
    assert result["messages"][-1]["content"] == "[SILENT]"


@pytest.mark.parametrize("candidate", ["", "[SILENT]"], ids=["blank", "marker"])
def test_withheld_quiet_candidate_is_not_a_budget_fallback(real_loop, monkeypatch, candidate):
    real_loop.agent.max_iterations = 2
    path = real_loop.ledger.with_suffix(".py")
    write = recovery._call("write_file", {"path": str(path), "content": "value = 1\n"}, "call_write")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    monkeypatch.setattr("agent.verification_stop._verification_snapshot", lambda **_: ({"status": "unverified"}, {}))
    summary = Mock(return_value="Verification is still pending.")
    monkeypatch.setattr(real_loop.agent, "_handle_max_iterations", summary)
    result, requests = _run_notice(real_loop, monkeypatch, _tools(write), recovery._response(candidate))
    assert len(requests) == 2
    assert not result["completed"]
    assert "Verification is still pending." in result["final_response"]
    summary.assert_called_once()


@pytest.mark.parametrize("content", ["", "[SILENT]"], ids=["blank", "marker"])
def test_anthropic_pause_cannot_settle_notice(real_loop, monkeypatch, content):
    real_loop.agent.api_mode = "anthropic_messages"

    def response(text, reason):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=reason, usage=None)

    result, requests = _run_notice(real_loop, monkeypatch, response(content, "pause_turn"),
                                   response("The operation is paused.", "end_turn"))
    assert len(requests) == 2
    assert result["final_response"] == "The operation is paused."


@pytest.mark.parametrize("stop_reason", [None, "nonterminal", "end_turn"])
def test_converse_stop_requires_native_completion(real_loop, monkeypatch, stop_reason):
    real_loop.agent.api_mode = "bedrock_converse"

    def response(text, reason):
        raw = {"output": {"message": {"content": [{"text": text}]}}}
        if reason is not None:
            raw["stopReason"] = reason
        return normalize_converse_response(raw)

    result, requests = _run_notice(real_loop, monkeypatch, response("", stop_reason),
                                   response("The work is not confirmed.", "end_turn"))
    if stop_reason == "end_turn":
        assert len(requests) == 1
        assert result["messages"][-1]["content"] == "[SILENT]"
    else:
        assert len(requests) == 2
        assert result["final_response"] == "The work is not confirmed."
