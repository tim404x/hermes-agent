"""effort-router delegation backend: per-task reasoning effort, tiered by how hard the task is.

The parent may set ``effort`` per task; when it does not, a cheap heuristic sends short mechanical goals to
medium; anything touching money, legal, sends, code changes or research synthesis never runs below high.
The same schema, guidance and heuristic apply at every depth (an orchestrator child's own delegate_task).
"""

import json
import types
from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from tools.delegate_tool_child_run import _route_report

REPO_PLUGINS = Path(__file__).resolve().parents[2] / "plugins"


@pytest.fixture
def home(tmp_path, monkeypatch):
    def make(delegation: dict):
        home = tmp_path / ".hermes"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({"delegation": delegation}), encoding="utf-8")
        bundled = tmp_path / "bundled"
        bundled.mkdir(exist_ok=True)
        if not (bundled / "delegation").exists():
            (bundled / "delegation").symlink_to(REPO_PLUGINS / "delegation", target_is_directory=True)
        monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(plugins_mod, "get_bundled_plugins_dir", lambda: bundled)
        manager = PluginManager()
        manager.discover_and_load()
        return manager
    managers = []
    yield lambda delegation: managers.append(make(delegation)) or managers[-1]
    for manager in managers:
        manager.unload()


@pytest.fixture
def recorded(monkeypatch):
    from tools import delegate_tool as dt
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return types.SimpleNamespace(tool_progress_callback=None, model=kwargs.get("model"), provider="openrouter",
                                     reasoning_config={"enabled": True, "effort": kwargs["override_reasoning_effort"]})

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", build)
    monkeypatch.setattr(dt, "_run_single_child", lambda task_index, goal, child=None, parent_agent=None, **kw: {
        "task_index": task_index, "status": "completed", "summary": "ok", "api_calls": 1, "duration_seconds": 0,
        "route": _route_report(child)})
    return built


def _parent(depth=0):
    return types.SimpleNamespace(session_id="root", model="parent-model", provider="openrouter", tool_progress_callback=None,
                                 _delegate_spinner=None, request_overrides={}, _safe_print=lambda line: None,
                                 _delegate_depth=depth)


def _routes(tasks, depth=0):
    from tools.delegate_tool_config import _load_config
    from tools.delegate_tool_routes import resolve_task_routes
    routes, err = resolve_task_routes(tasks, _load_config(), _parent(depth))
    assert err is None and not any(r.error for r in routes), [r.error for r in routes]
    return [(r.reasoning_effort, r.label) for r in routes]


EASY = ["Fetch the page title of https://example.com/about", "List the files in /tmp/run-1119/out",
        "Rename notes.md to notes-final.md in the run dir", "Look up the CNPJ of WeWork Faria Lima 4055"]
SENSITIVE = ["Draft the reply email to Figure about invoice 1023", "Fix the failing test in tools/delegate_tool.py",
             "Research and synthesize what labs pay for egocentric video", "Check the SOW terms Jackson sent",
             "Fetch the Mercury balance, then wire 500 USD to Nox", "Send the Slack update to Theo"]


def test_omitted_effort_easy_mechanical_goals_run_medium_and_say_why(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": g} for g in EASY]) == [("medium", "effort medium (auto: mechanical)")] * len(EASY)


@pytest.mark.parametrize("goal", SENSITIVE)
def test_money_legal_sends_code_and_research_never_run_below_high(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    [(effort, label)] = _routes([{"goal": goal}])
    assert effort == "high" and label == "effort high (default)"
    for asked in ("low", "medium"):  # an explicit cheap pick is floored too
        assert _routes([{"goal": goal, "effort": asked}]) == [("high", f"effort high (floor: asked {asked})")]


def test_explicit_picks_win_on_neutral_tasks_and_xhigh_is_never_automatic(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    goal = "Fetch the page title of https://example.com/about"
    assert _routes([{"goal": goal, "effort": "low"}, {"goal": goal, "effort": "xhigh"},
                    {"goal": "Think through the three options for the onboarding flow and pick one"}]) == [
        ("low", "effort low"), ("xhigh", "effort xhigh"), ("high", "effort high (default)")]
    assert _routes([{"goal": goal, "context": "background: the April invoice and the payout rail"}]) == [
        ("high", "effort high (default)")]  # sensitive context keeps it at the default


def test_a_cheap_profile_default_is_still_floored_for_sensitive_work(home):
    home({"backend": "effort-router", "reasoning_effort": "medium"})
    assert _routes([{"goal": "Send the Slack update to Theo"}]) == [("high", "effort high (floor: default medium)")]


def test_every_level_gets_the_effort_param_the_guidance_and_the_heuristic(home, recorded):
    home({"backend": "effort-router", "reasoning_effort": "high", "max_spawn_depth": 2})
    from tools.delegate_tool import delegate_task
    from tools.registry import registry
    definition = registry.get_definitions({"delegate_task"})[0]["function"]
    effort = definition["parameters"]["properties"]["tasks"]["items"]["properties"]["effort"]
    assert effort["enum"] == ["low", "medium", "high", "xhigh"]
    assert "medium" in effort["description"] and "never below high" in effort["description"]
    assert "never below high" in definition["description"]  # the backend's guidance rides the tool description
    # An orchestrator child (depth 1) delegating its own sub-subtasks: same routing, same heuristic.
    out = json.loads(delegate_task(tasks=[{"goal": EASY[0]}, {"goal": SENSITIVE[1]}, {"goal": EASY[1], "effort": "low"}],
                                   parent_agent=_parent(depth=1)))
    assert [r["status"] for r in out["results"]] == ["completed"] * 3
    assert [b["override_reasoning_effort"] for b in recorded] == ["medium", "high", "low"]
    # Each result says what its child ran at and why; the call reports the swarm's effort mix.
    assert [r["route"]["label"] for r in out["results"]] == [
        "effort medium (auto: mechanical)", "effort high (default)", "effort low"]
    assert out["effort_mix"] == {"medium": 1, "high": 1, "low": 1}
