"""#157662 on the desktop path: a prompt queued WHILE a tool turn runs is stored after that turn.

The busy accept writes the queued user row at accept time (``_persist_queued_user_row``), i.e.
between the live turn's tool rows. The drain (``_drain_queued_prompt``) must leave the active
transcript in send order, and the drained turn's first request must extend the previous turn's
last request plus its final assistant message as an exact prefix (no prompt-cache rewrite).

Real ``AIAgent`` (only the provider call is stubbed), real store, real tui_gateway queue/drain.
"""

from __future__ import annotations

import sqlite3
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from persistence.conversation.messages import ConversationRequest
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import conversation
from tests.agent.test_run_agent import _mock_response
from tui_gateway import server

_TOOL = "queued_order_probe"
_QUEUED = "queued while busy QUEUED-157662"


def _desktop_session(monkeypatch, db):
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda _sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda _session: None)
    resp = server.handle_request({"id": "c", "method": "session.create", "params": {"cols": 96, "source": "desktop"}})
    assert "result" in resp, resp
    sid = resp["result"]["session_id"]
    server._sessions[sid]["agent"] = types.SimpleNamespace()
    return sid, resp["result"]["stored_session_id"]


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
            skip_context_files=True, skip_memory=True, persistence=db, session_id=session_id,
        )
    agent.client = MagicMock()
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._session_db_created = True
    return agent


def _strip(messages):
    keep = ("role", "content", "tool_calls", "tool_call_id")
    return [{k: (str(m[k]) if k == "tool_calls" else m[k]) for k in keep if m.get(k) is not None}
            for m in messages if m.get("role") != "system"]


def _first_divergence(a, b):
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return None if len(b) >= len(a) else len(b)


def _active_rows(db_path, key):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT id, role, content FROM messages WHERE session_id = ? AND active = 1 ORDER BY id",
                            (key,)).fetchall()
    finally:
        conn.close()


def _run_queued_flow(monkeypatch, tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    sid, key = _desktop_session(monkeypatch, db)
    session = server._sessions[sid]
    server._ensure_session_db_row(session)
    agent = _agent(db, key)
    payloads: list[list[dict]] = []
    state = {"turn": 1, "calls": 0}

    def model_call(api_kwargs):
        payloads.append([dict(m) for m in api_kwargs["messages"]])
        state["calls"] += 1
        if state["turn"] == 1 and state["calls"] <= 2:
            if state["calls"] == 2:
                # The user sends while the tool turn runs: the busy accept writes its row NOW,
                # between the live turn's already-flushed rows and the rows still to come.
                with session["history_lock"]:
                    session["running"] = True
                    server._start_inflight_turn(session, "run the long task")
                resp = server._handle_busy_submit("r1", sid, session, _QUEUED, "ws-1", queued=True, display_kind=None)
                assert resp["result"]["status"] == "queued"
            tc = SimpleNamespace(id=f"call_{state['calls']}", type="function",
                                 function=SimpleNamespace(name=_TOOL, arguments="{}"))
            return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[tc])
        if state["turn"] == 1:
            return _mock_response(content="turn one done", finish_reason="stop")
        return _mock_response(content="queued turn done", finish_reason="stop")

    agent._interruptible_api_call = model_call
    out = SimpleNamespace(db=db, db_path=tmp_path / "state.db", key=key, payloads=payloads, sid=sid)

    def fake_submit(rid, s, sess, text, **kw):
        try:
            _fake_submit(rid, s, sess, text, **kw)
        except BaseException:  # surfaced by the assertions below
            import traceback
            out.submit_error = traceback.format_exc()
            raise

    def _fake_submit(rid, s, sess, text, **kw):
        # The body of the real ``_run_prompt_submit`` that matters here: adopt out-of-band rows into
        # the committed history, adopt the re-placed row, run the turn on that history.
        server._adopt_out_of_band_turns(sess)
        server._adopt_submit_user_row(sess, agent, text, text)
        state["turn"] = 2
        out.second = agent.run_conversation(text, conversation_history=list(sess["history"]))

    with (
        patch("model_tools.handle_function_call", return_value='{"ok": true}'),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        version = int(session.get("history_version", 0))
        out.first = agent.run_conversation("run the long task")
        assert server._commit_turn_history(session, out.first, [], version) is None
        with session["history_lock"]:
            session["running"] = False
            server._clear_inflight_turn(session)
        monkeypatch.setattr(server, "_run_prompt_submit", fake_submit)
        assert server._drain_queued_prompt("r2", sid, session) is True
    return out


def test_queued_user_row_is_stored_after_the_previous_turns_rows(monkeypatch, tmp_path):
    run = _run_queued_flow(monkeypatch, tmp_path)
    assert not getattr(run, "submit_error", None), run.submit_error
    try:
        rows = _active_rows(run.db_path, run.key)
        roles = [r[1] for r in rows]
        q = [i for i, r in enumerate(rows) if r[1] == "user" and _QUEUED in str(r[2])]
        assert len(q) == 1, roles  # exactly one active copy of the queued prompt
        assert roles[: q[0]] == ["user", "assistant", "tool", "assistant", "tool", "assistant"], roles
        assert rows[q[0] - 1][2] == "turn one done"
        assert roles[q[0]:] == ["user", "assistant"], roles
        # The mechanism under test really ran: the accept-time row was written INSIDE the live turn
        # (a smaller id than that turn's final assistant row) and survives only as an inactive row.
        conn = sqlite3.connect(f"file:{run.db_path}?mode=ro", uri=True)
        try:
            early = conn.execute("SELECT id FROM messages WHERE session_id = ? AND active = 0 AND content = ?",
                                 (run.key, _QUEUED)).fetchall()
        finally:
            conn.close()
        assert len(early) == 1 and early[0][0] < rows[q[0] - 1][0]
    finally:
        server._sessions.pop(run.sid, None)
        run.db.close()


def _shape(messages):
    return [(m.get("role"), m.get("content") or None, m.get("tool_call_id")) for m in messages
            if m.get("role") != "system"]


def test_durable_replay_extends_previous_last_request(monkeypatch, tmp_path):
    """A rebuild from the store (cold resume, gateway reload: ``read_conversation`` in row order)
    replays the previous turn's last request + its final answer as an exact prefix, then the
    queued prompt. With the queued row stored inside the previous turn this diverged at index 3."""
    run = _run_queued_flow(monkeypatch, tmp_path)
    assert not getattr(run, "submit_error", None), run.submit_error
    try:
        expected = _shape(run.payloads[2]) + [("assistant", "turn one done", None)]
        replay = _shape(conversation(run.db, run.key, ConversationRequest(repair_alternation=True)))
        assert _first_divergence(expected, replay) is None, (expected, replay)
        assert replay[len(expected)][:2] == ("user", _QUEUED)
    finally:
        server._sessions.pop(run.sid, None)
        run.db.close()


def test_queued_turn_first_request_extends_previous_last_request(monkeypatch, tmp_path):
    run = _run_queued_flow(monkeypatch, tmp_path)
    assert not getattr(run, "submit_error", None), run.submit_error
    try:
        assert len(run.payloads) == 4  # 3 provider calls for the tool turn, 1 for the queued turn
        expected_prefix = _strip(run.payloads[2]) + [{"role": "assistant", "content": "turn one done"}]
        first_next = _strip(run.payloads[3])
        assert _first_divergence(expected_prefix, first_next) is None, (expected_prefix, first_next)
        assert first_next[len(expected_prefix)]["role"] == "user"
        assert _QUEUED in str(first_next[len(expected_prefix)]["content"])
    finally:
        server._sessions.pop(run.sid, None)
        run.db.close()
