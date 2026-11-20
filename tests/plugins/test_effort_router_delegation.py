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
        "Look up the CNPJ of WeWork Faria Lima 4055", "Count the rows in /tmp/run-1119/out/ledger.csv",
        "Grep /tmp/run-1119/logs for TimeoutError"]
SENSITIVE = ["Draft the reply email to Figure about invoice 1023", "Fix the failing test in tools/delegate_tool.py",
             "Research and synthesize what labs pay for egocentric video", "Check the SOW terms Jackson sent",
             "Fetch the Mercury balance, then wire 500 USD to Nox", "Send the Slack update to Theo"]
# Every input the adversarial review (19 Nov 2026) ran: each one auto-lowered (or kept an explicit low) before.
REVIEWED = [
    "Read src/auth.py and replace the password check with True.",
    "Check the balance and transfer 500 GBP to Nox.",
    "Check whether Clause 4 permits unilateral termination.",
    "Transfira 500 reais para a conta da Nox",
    "Get the Coinbase USDC balance and transfer 50k to Dots",
    "Move money from Mercury checking to the Nox treasury account",
    "Move funds from Mercury to Nox",
    "Check whether the transfer to Datoric landed and if not resend it",
    "Find old backups in ~/backups and remove them",
    "Find and delete the stale S3 objects under s3://hub-prod/raw/",
    "Open Gmail and forward the thread to dani@hub.xyz",
    "Copy the deck to Drive and share it with jeremy@anthropic.com",
    "Open LinkedIn and connect with these 20 people",
    "Rename get_cost to get_spend across the hermes-agent repo",
    "Move tools/x.py into tools/x/ and update imports",
    "Grep the codebase for hardcoded API keys and rotate them",
    "Read le contrat Figure et résume les obligations",
]
# Sensitive or state-changing work in the other languages the team writes in (pt/fr/es).
MULTILINGUAL = [
    "Apague os backups antigos da pasta /tmp/old", "Envie o relatório para o Théo no Slack",
    "Supprime les anciens fichiers du dossier /tmp/old", "Envoie le message à Théo",
    "Transfiere 200 dólares a la cuenta de Nox", "Borra los archivos viejos de /tmp/old",
    "Lee el contrato y firma la cláusula 4",
]
# Read-only but not ONE mechanical operation, or not recognisably English: never auto-lowered.
NOT_MECHANICAL = [
    "List the files in /tmp/run-1119/out and count them", "Fetch the page title, then the meta description",
    "Find which of the three vendors is cheapest", "Liste os arquivos em /tmp/run-1119/out",
    "List les fichiers dans /tmp/run-1119/out", "ファイルの一覧を表示して",
    "Rename notes.md to notes-final.md in the run dir", "Format the JSON in /tmp/run-1119/out.json",
]


def test_omitted_effort_easy_mechanical_goals_run_medium_and_say_why(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": g} for g in EASY]) == [("medium", "effort medium (auto: mechanical)")] * len(EASY)


@pytest.mark.parametrize("goal", NOT_MECHANICAL)
def test_compound_write_or_unrecognised_goals_keep_the_default(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": goal}])[0][0] == "high"


@pytest.mark.parametrize("goal", SENSITIVE + REVIEWED + MULTILINGUAL)
def test_money_legal_sends_code_writes_and_research_never_run_below_high(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    [(effort, label)] = _routes([{"goal": goal}])
    assert effort == "high" and label == "effort high (default)"
    for asked in ("low", "medium"):  # an explicit cheap pick is floored too
        [(effort, label)] = _routes([{"goal": goal, "effort": asked}])
        assert effort == "high" and label.startswith(f"effort high (floor: asked {asked}")


@pytest.mark.parametrize("goal", REVIEWED[:4])
def test_a_sensitive_context_floors_an_explicit_pick_on_a_neutral_goal(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    task = {"goal": "Fetch the page title of https://example.com/about", "context": goal, "effort": "low"}
    assert _routes([task])[0][0] == "high"


def test_explicit_picks_win_on_neutral_tasks_and_xhigh_is_never_automatic(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    goal = "Fetch the page title of https://example.com/about"
    assert _routes([{"goal": goal, "effort": "low"}, {"goal": goal, "effort": "xhigh"},
                    {"goal": "Think through the three options for the onboarding flow and pick one"},
                    {"goal": EASY[1], "effort": "medium"}]) == [
        ("low", "effort low"), ("xhigh", "effort xhigh"), ("high", "effort high (default)"), ("medium", "effort medium")]
    assert _routes([{"goal": goal, "context": "background: the April invoice and the payout rail"}]) == [
        ("high", "effort high (default)")]  # sensitive context keeps it at the default


@pytest.mark.parametrize("default", ["medium", "low", "minimal", "none", "", False, None])
def test_a_cheap_empty_or_disabled_default_is_still_floored_for_sensitive_work(home, default):
    from tools.delegate_tool_config import _child_reasoning, _load_config
    home({"backend": "effort-router", "reasoning_effort": default})
    [(effort, label)] = _routes([{"goal": "Send the Slack update to Theo"}])
    assert effort == "high" and label.startswith("effort high (floor: default")
    # The route is explicit, so a low parent or a disabled default can never leak into the child.
    low_parent = types.SimpleNamespace(reasoning_config={"enabled": True, "effort": "low"})
    assert _child_reasoning(low_parent, _load_config(), effort) == {"enabled": True, "effort": "high"}


def test_an_xhigh_default_stays_xhigh_for_sensitive_work(home):
    home({"backend": "effort-router", "reasoning_effort": "xhigh"})
    assert _routes([{"goal": "Send the Slack update to Theo"}]) == [("xhigh", "effort xhigh (default)")]


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
