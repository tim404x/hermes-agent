"""Delegation-tree spend rolls up to the parent (issue #92004: views read one row, undercounting ~2.3x).

A child's own token counters miss everything it delegated to, so a depth-2 swarm's report showed a fraction
of its real spend. Each finished batch now folds its children's whole subtree (tokens + cost + subagent
count) into the parent's ``delegation_tree_usage``, and the delegate_task result carries the call's total.
"""

import json
import threading
from unittest.mock import MagicMock, patch

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


def test_later_calls_add_to_the_session_total_and_a_continued_child_counts_only_new_spend():
    root, child = _agent(0), _agent(1, cost=0.25, inp=100, out=5)
    assert _delegate(root, [child], 1)["usage_total"]["input_tokens"] == 100
    child.session_input_tokens, child.session_estimated_cost_usd = 160, 0.4  # the same child, continued
    again = _delegate(root, [child], 1)["usage_total"]
    assert (again["input_tokens"], again["cost_usd"], again["subagents"]) == (60, 0.15, 0)
    assert (root.delegation_tree_usage["input_tokens"], root.delegation_tree_usage["subagents"]) == (160, 1)


def test_a_child_run_without_a_live_parent_still_reports_its_spend():
    assert _rollup_tree_usage([_agent(1, cost=0.5, inp=40)], None)["input_tokens"] == 40
