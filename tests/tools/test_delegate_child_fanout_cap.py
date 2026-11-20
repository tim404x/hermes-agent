"""delegation.max_children_per_child: the depth-2 fan-out bound.

max_concurrent_children caps one batch, but every orchestrator child gets the same cap for its own
batches, so 80 top-level children could each start 80 more (6,400 agents). A subagent's own
delegate_task call is capped separately; the top level keeps max_concurrent_children.
"""

import json
import threading
from unittest.mock import MagicMock

import pytest

from hermes_constants import get_hermes_home
from tools import delegate_tool


def _config(extra: str = "") -> None:
    (get_hermes_home() / "config.yaml").write_text(
        "delegation:\n  max_spawn_depth: 3\n  max_concurrent_children: 80\n" + extra, encoding="utf-8")


def _parent(depth: int):
    parent = MagicMock()
    parent.base_url, parent.api_key, parent.provider = "https://openrouter.ai/api/v1", "k", "openrouter"
    parent.api_mode, parent.model, parent.platform = "chat_completions", "anthropic/claude-sonnet-4", "cli"
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = parent.provider_sort = None
    parent._session_db, parent._delegate_depth = None, depth
    parent._active_children, parent._active_children_lock = [], threading.Lock()
    parent._print_fn = parent.tool_progress_callback = parent.thinking_callback = None
    return parent


@pytest.fixture
def built(monkeypatch):
    """Stops delegate_task right where children would be built; records the batch size that got there."""
    calls = []
    monkeypatch.setattr(delegate_tool, "_build_children",
                        lambda task_list, *a, **k: (calls.append(len(task_list)), ([], "REACHED_BUILD"))[1])
    return calls


def _call(depth: int, n: int) -> dict:
    tasks = [{"goal": f"look up record number {i} in the ledger"} for i in range(n)]
    return json.loads(delegate_tool.delegate_task(tasks=tasks, parent_agent=_parent(depth)))


def test_a_subagent_batch_over_the_cap_is_refused_before_any_child_is_built(built):
    _config("  max_children_per_child: 3\n")
    out = _call(depth=1, n=4)
    assert "delegation.max_children_per_child" in out["error"]
    assert "4" in out["error"] and "3" in out["error"]
    assert built == []


def test_the_cap_binds_every_subagent_level_but_never_the_top_level(built):
    _config("  max_children_per_child: 3\n")
    assert _call(depth=2, n=4)["error"].count("max_children_per_child") == 1
    assert _call(depth=0, n=4)["error"] == "REACHED_BUILD"  # the user's own orchestrator keeps 80
    assert _call(depth=1, n=3)["error"] == "REACHED_BUILD"
    assert built == [4, 3]


@pytest.mark.parametrize("extra", ["", "  max_children_per_child: 0\n", "  max_children_per_child: nope\n"])
def test_unset_zero_or_invalid_keeps_todays_behaviour(built, extra):
    _config(extra)
    assert _call(depth=1, n=12)["error"] == "REACHED_BUILD"
    assert built == [12]


def test_the_schema_states_the_per_child_cap_only_when_it_is_set():
    _config("  max_children_per_child: 10\n")
    assert "max_children_per_child=10" in delegate_tool._build_dynamic_schema_overrides()["description"]
    _config()
    assert "max_children_per_child" not in delegate_tool._build_dynamic_schema_overrides()["description"]
