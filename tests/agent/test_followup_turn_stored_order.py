"""#157662: a message sent while a turn runs must be stored AFTER that turn's rows.

The follow-up (a leftover /steer handed back as ``result["pending_steer"]``, or a queued
message drained after the turn) runs as the next turn on the previous turn's in-memory
``result["messages"]`` (gateway/run_turn.py ``_run_agent_queued_followup``; tui_gateway's
committed ``session["history"]``). Two contracts keep the provider prompt cache warm:

(a) stored order == send order: ``ORDER BY id`` of the active rows equals the order the
    messages were appended, so a later rebuild from state.db replays the same prefix;
(b) the follow-up's first request starts with the previous turn's last request plus its
    final assistant message as an exact prefix.

Real ``AIAgent`` + real SessionDB; only the provider call is stubbed.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from tests.agent.test_run_agent import _mock_response

_TOOL = "followup_order_probe"
_STEER = "also check the logs (sent mid-turn)"


def _agent(db, session_id):
    from run_agent import AIAgent

    schema = {"type": "function", "function": {
        "name": _TOOL, "description": "probe", "parameters": {"type": "object", "properties": {}}}}
    with (
        patch("model_tools.get_tool_definitions", return_value=[schema]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True, session_db=db, session_id=session_id,
        )
    agent.client = MagicMock()
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _tool_call(n):
    return SimpleNamespace(id=f"call_{n}", type="function",
                           function=SimpleNamespace(name=_TOOL, arguments="{}"))


def _strip(messages):
    """Wire-relevant view of a request (drop the system prompt; it is rebuilt per turn)."""
    keep = ("role", "content", "tool_calls", "tool_call_id")
    out = []
    for m in messages:
        if m.get("role") == "system":
            continue
        out.append({k: (str(m[k]) if k == "tool_calls" else m[k]) for k in keep if m.get(k) is not None})
    return out


def _first_divergence(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(b) >= len(a) else len(b)


def _active_rows(db_path, session_id):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT id, role, content, tool_call_id FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
            (session_id,)).fetchall()
    finally:
        conn.close()


def _run_turn_with_midturn_message(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    session_id = "sess-157662"
    db.create_session(session_id=session_id, source="desktop", model="stub")
    agent = _agent(db, session_id)
    agent._session_db_created = True
    payloads: list[list[dict]] = []
    turn_of_call: list[int] = []
    turn = {"n": 1}

    def model_call(api_kwargs):
        payloads.append([dict(m) for m in api_kwargs["messages"]])
        turn_of_call.append(turn["n"])
        calls_this_turn = turn_of_call.count(turn["n"])
        if turn["n"] == 1 and calls_this_turn <= 2:
            return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[_tool_call(calls_this_turn)])
        if turn["n"] == 1:
            # The user's message lands while the final answer is generating: it has no tool batch
            # to ride, so the finalizer hands it back as the next turn (result["pending_steer"]).
            assert agent.steer(_STEER) is True
            return _mock_response(content="turn one done", finish_reason="stop")
        return _mock_response(content="follow-up done", finish_reason="stop")

    agent._interruptible_api_call = model_call
    with (
        patch("model_tools.handle_function_call", return_value='{"ok": true}'),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        first = agent.run_conversation("run the long task")
        followup_text = first.get("pending_steer")
        assert followup_text and _STEER in followup_text
        turn["n"] = 2
        # Exactly what both gateways do: the follow-up turn runs on the previous turn's in-memory list.
        second = agent.run_conversation(followup_text, conversation_history=first["messages"])
    return SimpleNamespace(db=db, db_path=db_path, session_id=agent.session_id, payloads=payloads,
                           turn_of_call=turn_of_call, first=first, second=second, followup_text=followup_text)


def test_followup_user_row_is_stored_after_every_row_of_the_previous_turn(tmp_path):
    run = _run_turn_with_midturn_message(tmp_path)
    try:
        rows = _active_rows(run.db_path, run.session_id)
        roles = [r[1] for r in rows]
        follow_idx = next(i for i, r in enumerate(rows) if r[1] == "user" and _STEER in str(r[2]))
        prev_turn = rows[:follow_idx]
        # Every assistant/tool row of the previous turn sits BEFORE the follow-up's user row.
        assert [r[1] for r in prev_turn] == ["user", "assistant", "tool", "assistant", "tool", "assistant"], roles
        assert prev_turn[-1][2] == "turn one done"
        assert [r[1] for r in rows[follow_idx:]] == ["user", "assistant"], roles
        # ORDER BY id == append order of the in-memory transcript the follow-up returned.
        mem = [m for m in run.second["messages"] if m.get("role") in ("user", "assistant", "tool")]
        assert [(m["role"], m.get("content") or None) for m in mem] == [(r[1], r[2] or None) for r in rows]
    finally:
        run.db.close()


def test_followup_first_request_extends_previous_last_request_exactly(tmp_path):
    run = _run_turn_with_midturn_message(tmp_path)
    try:
        assert run.turn_of_call == [1, 1, 1, 2]  # 3 provider calls for the tool turn, 1 for the follow-up
        last_prev = _strip(run.payloads[2])
        first_next = _strip(run.payloads[3])
        expected_prefix = last_prev + [{"role": "assistant", "content": "turn one done"}]
        assert _first_divergence(expected_prefix, first_next) is None, (expected_prefix, first_next)
        assert first_next[: len(expected_prefix)] == expected_prefix
        assert first_next[len(expected_prefix)]["role"] == "user"
        assert _STEER in str(first_next[len(expected_prefix)]["content"])
    finally:
        run.db.close()
