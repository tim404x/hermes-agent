"""Delegation-tree spend rolls up to the parent (issue #92004: views read one row, undercounting ~2.3x).

A child's own token counters miss everything it delegated to, so a depth-2 swarm's report showed a fraction
of its real spend. Each finished batch now folds its children's whole subtree (tokens + cost + subagent
count) into the parent's ``delegation_tree_usage``, and the delegate_task result carries the call's total.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import get_hermes_home
from tools.delegate_tool import delegate_task
from tools.delegate_tool_results import _rollup_tree_usage


def _agent(depth=0, *, cost=0.0, inp=0, out=0, cache_read=0, reasoning=0):
    agent = MagicMock()
    agent.base_url, agent.api_key, agent.provider = "https://openrouter.ai/api/v1", "k", "openrouter"
    agent.api_mode, agent.model, agent.platform = "chat_completions", "anthropic/claude-sonnet-4", "cli"
    agent.providers_allowed = agent.providers_ignored = agent.providers_order = agent.provider_sort = None
    agent._session_db, agent._delegate_depth = None, depth
    agent._active_children, agent._active_children_lock = [], threading.Lock()
    agent._print_fn = agent.tool_progress_callback = agent.thinking_callback = None
    agent.session_cost_source, agent.session_cost_status = "none", "estimated"
    agent.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1, "messages": []}
    agent.session_prompt_tokens, agent.session_completion_tokens = inp + cache_read, out
    agent.session_input_tokens, agent.session_output_tokens = inp, out
    agent.session_cache_read_tokens, agent.session_cache_write_tokens = cache_read, 0
    agent.session_reasoning_tokens, agent.session_estimated_cost_usd = reasoning, cost
    agent.delegation_tree_usage = None
    return agent


def _delegate(parent, children, n):
    with patch("run_agent.AIAgent", side_effect=children):
        tasks = [{"goal": f"look up record number {i} in the ledger"} for i in range(n)]
        return json.loads(delegate_task(tasks=tasks, parent_agent=parent))


def test_a_depth_two_swarm_reports_and_rolls_up_every_level():
    (get_hermes_home() / "config.yaml").write_text("delegation:\n  max_spawn_depth: 2\n", encoding="utf-8")
    root = _agent(0, cost=1.0, inp=100)
    orchestrator = _agent(1, cost=2.0, inp=1000, out=50, cache_read=5000)
    workers = [_agent(2, cost=0.5, inp=300, out=10, cache_read=700, reasoning=5) for _ in range(2)]

    # The orchestrator child's own batch (depth 1 -> 2) finishes first.
    inner = _delegate(orchestrator, workers, 2)
    assert inner["usage_total"] == {"subagents": 2, "input_tokens": 600, "output_tokens": 20,
                                    "cache_read_tokens": 1400, "cache_write_tokens": 0, "reasoning_tokens": 10,
                                    "cost_usd": 1.0}
    assert orchestrator.session_estimated_cost_usd == 3.0  # the existing cost rollup, unchanged

    # The root's call sees the orchestrator's own spend PLUS everything below it.
    outer = _delegate(root, [orchestrator], 1)
    whole_tree = {"subagents": 3, "input_tokens": 1600, "output_tokens": 70, "cache_read_tokens": 6400,
                  "cache_write_tokens": 0, "reasoning_tokens": 10, "cost_usd": 3.0}
    assert outer["usage_total"] == whole_tree
    assert root.delegation_tree_usage == whole_tree
    assert root.session_estimated_cost_usd == 4.0


def test_later_calls_add_to_the_session_total():
    root = _agent(0)
    assert _delegate(root, [_agent(1, cost=0.25, inp=100, out=5)], 1)["usage_total"]["input_tokens"] == 100
    again = _delegate(root, [_agent(1, cost=0.15, inp=60)], 1)["usage_total"]
    assert (again["input_tokens"], again["cost_usd"], again["subagents"]) == (60, 0.15, 1)
    assert (root.delegation_tree_usage["input_tokens"], root.delegation_tree_usage["subagents"]) == (160, 2)


def test_a_real_continue_adds_its_new_spend_but_not_the_same_subagent_again():
    """action: continue rebuilds the child object for the same durable subagent: it is still one subagent."""
    from tests.tools.test_delegate_continue import _parent, _ScriptedModel

    parent, scripted = _parent(), _ScriptedModel()

    def model(**kwargs):
        child = scripted(**kwargs)
        child.session_input_tokens, child.session_output_tokens, child.session_estimated_cost_usd = 100, 5, 0.25
        return child

    try:
        with patch("run_agent.AIAgent", side_effect=model):
            first = json.loads(delegate_task(tasks=[{"goal": "list files"}], parent_agent=parent))
            sid = first["results"][0]["subagent_id"]
            second = json.loads(delegate_task(action="continue", subagent_id=sid, message="list more",
                                              parent_agent=parent))
        assert second["results"][0]["subagent_id"] == sid
        assert first["usage_total"]["subagents"] == 1
        assert (second["usage_total"]["input_tokens"], second["usage_total"]["cost_usd"],
                second["usage_total"]["subagents"]) == (100, 0.25, 0)
        assert (parent.delegation_tree_usage["input_tokens"], parent.delegation_tree_usage["subagents"]) == (200, 1)
    finally:
        parent.persistence.close()


def test_a_new_session_starts_its_tree_total_from_zero():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    _rollup_tree_usage([SimpleNamespace(session_input_tokens=100, session_estimated_cost_usd=0.5)], agent)
    agent.session_id = "new-session"
    agent.reset_session_state()
    _rollup_tree_usage([SimpleNamespace(session_input_tokens=10, session_estimated_cost_usd=0.1)], agent)
    usage = agent.delegation_tree_usage
    assert (usage["input_tokens"], usage["cost_usd"], usage["subagents"]) == (10, 0.1, 1)


def test_a_child_finishing_after_a_session_switch_stays_out_of_the_new_sessions_totals():
    root, child = _agent(0), _agent(1, cost=0.5, inp=100)
    root._session_reset_epoch = 1

    def switch(*_a, **_k):  # /new lands while the child runs (a background unit that finishes late)
        root._session_reset_epoch, root.delegation_tree_usage, root.session_estimated_cost_usd = 2, None, 0.0
        return {"final_response": "done", "completed": True, "api_calls": 1, "messages": []}

    child.run_conversation.side_effect = switch
    out = _delegate(root, [child], 1)
    assert out["usage_total"]["input_tokens"] == 100  # the call still reports its own spend
    assert not root.delegation_tree_usage and root.session_estimated_cost_usd == 0.0


def test_a_child_run_without_a_live_parent_still_reports_its_spend():
    assert _rollup_tree_usage([_agent(1, cost=0.5, inp=40)], None)["input_tokens"] == 40


def _bare_parent(has_warmer: bool):
    """A real AIAgent's session state (no __init__), with or without a prompt-cache warmer."""
    from run_agent import AIAgent

    parent = AIAgent.__new__(AIAgent)
    parent.session_id, parent._session_reset_epoch = "old", 0
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_source, parent.session_cost_status = "none", "unknown"
    if has_warmer:
        from agent.prompt_cache_lifecycle.warmer import get_prompt_cache_warmer
        get_prompt_cache_warmer(parent)
    return parent


def _finalize_old_batch(parent, out: list) -> None:
    """Finish one child built in the parent's session epoch 0: 100 input tokens, $0.50."""
    from tools.delegate_tool_results import _finalize_child_results

    child = SimpleNamespace(_delegate_parent_epoch=0, session_id="child", session_input_tokens=100,
                            session_estimated_cost_usd=0.5)
    entry = {"task_index": 0, "status": "completed", "summary": "done", "_child_cost_usd": 0.5}
    out.append(_finalize_child_results([entry], [{"goal": "list"}], [(0, {"goal": "list"}, child)], parent))


@pytest.mark.parametrize("has_warmer", [False, True])
def test_a_reset_while_a_batch_waits_for_the_accounting_lock_fences_the_batch_out(monkeypatch, has_warmer):
    """/new lands while an old batch's finalizer is about to take the parent's accounting lock (round-2 review,
    19 Nov 2026). The epoch is compared under that lock, so the old $0.50 and 100 tokens stay out of the new
    session, with or without a cache warmer; the call still reports its own spend."""
    from agent import prompt_cache_lifecycle

    parent = _bare_parent(has_warmer)
    requested, proceed = threading.Event(), threading.Event()
    real_lock = prompt_cache_lifecycle.session_totals_lock

    def paused(agent):  # a scheduler pause at the lock acquisition only: the real lock is returned
        if threading.current_thread().name == "old-batch":
            requested.set()
            assert proceed.wait(5)
        return real_lock(agent)

    monkeypatch.setattr(prompt_cache_lifecycle, "session_totals_lock", paused)
    out: list = []
    worker = threading.Thread(target=_finalize_old_batch, args=(parent, out), name="old-batch", daemon=True)
    worker.start()
    try:
        assert requested.wait(5)
        parent.session_id = "new"
        parent.reset_session_state()
    finally:
        proceed.set()
        worker.join(timeout=5)
    assert not worker.is_alive() and out and out[0]["input_tokens"] == 100
    assert parent.session_estimated_cost_usd == 0.0 and not parent.delegation_tree_usage


@pytest.mark.parametrize("has_warmer", [False, True])
def test_a_reset_waits_for_a_batch_that_already_passed_its_epoch_check(monkeypatch, has_warmer):
    """The other order: the old batch passed its epoch check and pauses before folding. The check and every
    fold are one critical section with reset_session_state, so /new waits for the fold and then zeroes it."""
    from tools import delegate_tool_results as results

    parent = _bare_parent(has_warmer)
    checked, proceed, reset_done = threading.Event(), threading.Event(), threading.Event()
    real_check = results._from_older_session

    def paused(children, agent):  # a scheduler pause right after the real check
        older = real_check(children, agent)
        if threading.current_thread().name == "old-batch":
            checked.set()
            assert proceed.wait(5)
        return older

    def reset() -> None:
        parent.session_id = "new"
        parent.reset_session_state()
        reset_done.set()

    monkeypatch.setattr(results, "_from_older_session", paused)
    out: list = []
    worker = threading.Thread(target=_finalize_old_batch, args=(parent, out), name="old-batch", daemon=True)
    resetter = threading.Thread(target=reset, daemon=True)
    worker.start()
    try:
        assert checked.wait(5)
        resetter.start()
        # Holding the lock across check and fold, the reset cannot finish until the batch has folded.
        assert not reset_done.wait(1.0)
    finally:
        proceed.set()
        worker.join(timeout=5)
        resetter.join(timeout=5)
    assert not worker.is_alive() and not resetter.is_alive() and reset_done.is_set() and out
    assert parent._session_reset_epoch == 1
    assert parent.session_estimated_cost_usd == 0.0 and not parent.delegation_tree_usage
