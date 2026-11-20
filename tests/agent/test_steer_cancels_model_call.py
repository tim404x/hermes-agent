"""display.steer_cancels_model_call: a steer sent while a model request is in flight cancels that
request after a debounce and the loop retries with every buffered text (a burst cancels once).
During tool execution, once a tool call is streaming, or with the flag off, steering is unchanged."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.prompt_builder import STEER_DISPLAY_KIND, STEER_MARKER_OPEN
from tools.registry import registry

_TOOL = "steer_cancel_probe"
_TOOL_HOOK: dict = {}


def _probe_tool(args, **_kwargs):
    hook = _TOOL_HOOK.get("during_tool")
    if hook is not None:
        hook()
    return "probe ok"


registry.register(
    name=_TOOL,
    toolset="utility",
    schema={
        "name": _TOOL,
        "description": "probe tool for the steer-cancel tests",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    handler=_probe_tool,
    override=True,
)

DEBOUNCE = 0.3
EPSILON = 0.6  # generous: thread wake-up + the stub's 10 ms interrupt poll on a loaded CI box


def _agent(enabled: bool):
    from run_agent import AIAgent

    tool_schema = {
        "type": "function",
        "function": {
            "name": _TOOL,
            "description": "probe tool for the steer-cancel tests",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
    with (
        patch("model_tools.get_tool_definitions", return_value=[tool_schema]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._steer_cancels_model_call = enabled
    agent._steer_cancel_debounce_s = DEBOUNCE
    return agent


def _tool_call_response(call_id="call_1"):
    from tests.agent.test_run_agent import _mock_response

    tool_call = SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=_TOOL, arguments="{}"),
    )
    return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[tool_call])


def _final(text):
    from tests.agent.test_run_agent import _mock_response

    return _mock_response(content=text, finish_reason="stop")


class _SlowModel:
    """Stub provider. ``slow_first`` makes call 1 a cancellable ``slow_s`` request (it polls the
    interrupt flag like the real interruptible call); ``first`` is what call 1 returns when it is
    not cancelled; later calls return ``final``."""

    def __init__(self, agent, *, slow_s=20.0, first=None, final=None, before_first_wait=None):
        self.agent = agent
        self.slow_s = slow_s
        self.first = first if first is not None else _final("uncancelled answer")
        self.final = final if final is not None else _final("answer with the steer")
        self.before_first_wait = before_first_wait
        self.requests: list = []
        self.request_started: list = []
        self.cancelled_at: list = []
        self.entered = threading.Event()

    def __call__(self, api_kwargs):
        self.requests.append([dict(m) for m in api_kwargs["messages"]])
        self.request_started.append(time.monotonic())
        if len(self.requests) == 1:
            if self.before_first_wait is not None:
                self.before_first_wait()
            self.entered.set()
            deadline = time.monotonic() + self.slow_s
            while time.monotonic() < deadline:
                if self.agent._interrupt_requested:
                    self.cancelled_at.append(time.monotonic())
                    raise InterruptedError("request cancelled")
                time.sleep(0.01)
            return self.first
        return self.final


def _run(agent, model, prompt="do the thing", **kw):
    agent._interruptible_api_call = model
    out = {}
    with (
        patch.object(agent, "_flush_messages_to_session_db"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        worker = threading.Thread(target=lambda: out.update(result=agent.run_conversation(prompt, **kw)))
        worker.start()
        yield  # caller sends the steers while the turn runs
        worker.join(timeout=30)
    assert not worker.is_alive()
    yield out["result"]


def _drive(agent, model, steer_plan, prompt="do the thing"):
    """Start the turn on a worker thread, wait until call 1 is in flight, then send each steer
    (``(delay_after_previous_s, text)``). Returns (result, steer_send_times)."""
    gen = _run(agent, model, prompt)
    next(gen)
    assert model.entered.wait(timeout=10)
    sent = []
    for delay, text in steer_plan:
        time.sleep(delay)
        sent.append(time.monotonic())
        assert agent.steer(text) is True
    result = next(gen)
    return result, sent


def _texts(rows):
    return "\n".join(str(r.get("api_content") or r.get("content") or "") for r in rows)


@pytest.fixture(autouse=True)
def _clear_tool_hook():
    _TOOL_HOOK.clear()
    yield
    _TOOL_HOOK.clear()


class TestSteerCancelsModelCall:
    def test_steer_mid_call_cancels_after_debounce_and_retries_with_it(self):
        agent = _agent(enabled=True)
        model = _SlowModel(agent)
        result, sent = _drive(agent, model, [(0.2, "use postgres")])

        assert len(model.cancelled_at) == 1
        latency = model.cancelled_at[0] - sent[0]
        assert DEBOUNCE - 0.05 <= latency <= DEBOUNCE + EPSILON, latency
        assert len(model.requests) == 2
        # The retried request carries the steer exactly once, as the last row.
        retry = model.requests[1]
        assert _texts(retry).count("use postgres") == 1
        assert retry[-1]["role"] == "user" and "use postgres" in str(retry[-1]["content"])
        # Single assistant answer; transcript order user -> placeholder -> steer row -> answer.
        assert result["completed"] is True
        assert result["final_response"] == "answer with the steer"
        msgs = result["messages"]
        assert [m["role"] for m in msgs[-4:]] == ["user", "assistant", "user", "assistant"]
        steer_row = msgs[-2]
        assert steer_row.get("display_kind") == STEER_DISPLAY_KIND
        assert str(steer_row["content"]).startswith(STEER_MARKER_OPEN)
        assert sum(_texts([m]).count("use postgres") for m in msgs) == 1
        assert [m for m in msgs if m["role"] == "assistant" and m.get("content") == "answer with the steer"]
        assert result.get("pending_steer") is None
        assert agent._pending_steer is None and agent._pending_redirect is None

    def test_burst_of_three_cancels_once_and_delivers_all(self):
        agent = _agent(enabled=True)
        model = _SlowModel(agent)
        result, sent = _drive(agent, model, [(0.2, "one"), (0.1, "two"), (0.1, "three")])

        assert len(model.cancelled_at) == 1
        assert len(model.requests) == 2
        # Trailing debounce: fires one debounce after the LAST message of the burst.
        assert DEBOUNCE - 0.05 <= model.cancelled_at[0] - sent[-1] <= DEBOUNCE + EPSILON
        last = str(model.requests[1][-1]["content"])
        assert last.index("one") < last.index("two") < last.index("three")
        msgs = result["messages"]
        for word in ("one", "two", "three"):
            assert sum(_texts([m]).count(word) for m in msgs if m["role"] == "user") == 1
        assert result["final_response"] == "answer with the steer"
        assert result.get("pending_steer") is None

    def test_steady_stream_is_capped_at_three_debounces(self):
        agent = _agent(enabled=True)
        model = _SlowModel(agent)
        plan = [(0.05, "msgA")] + [(0.2, f"m{i}x") for i in range(8)]  # keeps restarting the debounce
        result, sent = _drive(agent, model, plan)
        assert model.cancelled_at, "never cancelled"
        assert model.cancelled_at[0] - sent[0] <= 3 * DEBOUNCE + EPSILON
        # Every message lands exactly once: in the transcript, handed back as the next prompt, or
        # (sent after the turn ended, which the gateway would refuse) still in the buffer.
        all_text = "\n".join([
            _texts([m for m in result["messages"] if m["role"] == "user"]),
            str(result.get("pending_steer") or ""), str(agent._pending_steer or ""),
        ])
        for _, text in plan:
            assert all_text.count(text) == 1, text

    def test_flag_off_keeps_todays_behaviour(self):
        agent = _agent(enabled=False)
        model = _SlowModel(agent, slow_s=1.0)
        result, _ = _drive(agent, model, [(0.1, "late note")])

        assert model.cancelled_at == []
        assert len(model.requests) == 1
        assert result["final_response"] == "uncancelled answer"
        # Unchanged: the steer is handed back as the next turn's prompt.
        assert result["pending_steer"] == "late note"

    def test_steer_during_tool_is_unchanged(self):
        agent = _agent(enabled=True)
        model = _SlowModel(agent, slow_s=0.0, first=_tool_call_response())
        _TOOL_HOOK["during_tool"] = lambda: (agent.steer("note during tool"), time.sleep(DEBOUNCE * 2))
        agent._interruptible_api_call = model
        with (
            patch.object(agent, "_flush_messages_to_session_db"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("run the tool")

        assert model.cancelled_at == []
        assert len(model.requests) == 2
        second = model.requests[1]
        assert [m["role"] for m in second[-3:]] == ["assistant", "tool", "user"]
        assert "note during tool" in str(second[-1]["content"])
        assert result["final_response"] == "answer with the steer"
        assert result.get("pending_steer") is None

    def test_streaming_tool_call_is_not_cancelled(self):
        agent = _agent(enabled=True)
        model = _SlowModel(
            agent, slow_s=DEBOUNCE * 3, first=_tool_call_response(),
            before_first_wait=lambda: agent._fire_tool_gen_started(_TOOL),
        )
        result, _ = _drive(agent, model, [(0.05, "after the tool please")])

        assert model.cancelled_at == []
        assert len(model.requests) == 2
        second = model.requests[1]
        assert second[-2]["role"] == "tool"
        assert "after the tool please" in str(second[-1]["content"])
        assert result["final_response"] == "answer with the steer"


class TestSteerCancelUnits:
    def _bare(self, enabled=True, active=True):
        agent = _agent(enabled=enabled)
        if active:
            agent._model_request_active.set()
        return agent

    def test_fire_after_request_finished_keeps_the_steer_buffered(self):
        agent = self._bare()
        assert agent.steer("keep me") is True
        agent._model_request_active.clear()
        assert agent._fire_steer_cancel(agent._steer_cancel_gen) is False
        assert agent._pending_steer == "keep me"
        assert agent._pending_redirect is None
        assert agent._interrupt_requested is False

    def test_stale_timer_generation_is_a_no_op(self):
        agent = self._bare()
        agent.steer("first")
        stale = agent._steer_cancel_gen
        agent.steer("second")
        assert agent._fire_steer_cancel(stale) is False
        assert agent._pending_steer == "first\nsecond"
        agent._steer_cancel_timer.cancel()

    def test_fire_moves_ticketed_parts_and_marks_them_consumed(self):
        agent = self._bare()
        agent.steer("plain")
        agent.steer("transfer", ticket="t1")
        agent._steer_cancel_timer.cancel()
        assert agent._fire_steer_cancel(agent._steer_cancel_gen) is True
        assert agent._pending_steer is None
        assert agent._steer_ticket_fates == {"t1": True}
        assert agent._pending_redirect.startswith(STEER_MARKER_OPEN)
        assert "plain\ntransfer" in agent._pending_redirect
        assert agent._interrupt_requested is True

    def test_hard_stop_pending_refuses_and_keeps_buffer(self):
        agent = self._bare()
        agent.steer("x")
        agent._steer_cancel_timer.cancel()
        agent._interrupt_requested = True
        assert agent._fire_steer_cancel(agent._steer_cancel_gen) is False
        assert agent._pending_steer == "x"

    def test_no_timer_when_disabled_or_idle_or_executing_tools(self):
        assert self._bare(enabled=False)._maybe_arm_steer_cancel() is False
        idle = self._bare(active=False)
        idle.steer("x")
        assert idle._steer_cancel_timer is None
        busy = self._bare()
        busy._executing_tools = True
        busy.steer("x")
        assert busy._steer_cancel_timer is None
        streaming = self._bare()
        streaming._fire_tool_gen_started(_TOOL)
        streaming.steer("x")
        assert streaming._steer_cancel_timer is None
        # A new request resets the tool-call flag.
        streaming._reset_stream_delivery_tracking()
        assert streaming._model_streaming_tool_call is False


class TestSteerCancelConfig:
    def test_defaults_are_off(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["display"]["steer_cancels_model_call"] is False
        assert DEFAULT_CONFIG["display"]["steer_cancel_debounce_seconds"] == 1.5

    @pytest.mark.parametrize(
        ("display", "enabled", "debounce"),
        [
            ({}, False, 1.5),
            ({"steer_cancels_model_call": True, "steer_cancel_debounce_seconds": 2}, True, 2.0),
            ({"steer_cancels_model_call": "yes", "steer_cancel_debounce_seconds": "bad"}, True, 1.5),
            ({"steer_cancels_model_call": "false", "steer_cancel_debounce_seconds": -1}, False, 0.0),
        ],
    )
    def test_apply_display_config_reads_the_keys(self, display, enabled, debounce):
        from agent.agent_init import _apply_display_config

        agent = SimpleNamespace()
        try:
            _apply_display_config(agent, {"display": display, "agent": {}}, "cli")
        except Exception:
            pass  # later sections may need a full agent; ours run first
        assert agent._steer_cancels_model_call is enabled
        assert agent._steer_cancel_debounce_s == debounce
