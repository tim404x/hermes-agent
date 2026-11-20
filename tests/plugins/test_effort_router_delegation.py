"""effort-router delegation backend: per-task reasoning effort, tiered by how hard the task is.

The parent may set ``effort`` per task; when it does not, only a deterministic read runs at medium. Reviews,
plans, debugging and synthesis run at xhigh; anything touching money, legal, sends, code changes or writes never
runs below high; low is only for deterministic reads. The same schema, guidance and heuristic apply at every depth
(an orchestrator child's own delegate_task) and on every model option (``option``).
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


def _parent(depth=0, reasoning_config=None):
    return types.SimpleNamespace(session_id="root", model="parent-model", provider="openrouter", tool_progress_callback=None,
                                 _delegate_spinner=None, request_overrides={}, _safe_print=lambda line: None,
                                 _delegate_depth=depth, reasoning_config=reasoning_config)


def _routes(tasks, depth=0, parent=None):
    from tools.delegate_tool_config import _load_config
    from tools.delegate_tool_routes import resolve_task_routes
    routes, err = resolve_task_routes(tasks, _load_config(), parent or _parent(depth))
    assert err is None and not any(r.error for r in routes), [r.error for r in routes]
    return [(r.reasoning_effort, r.label) for r in routes]


EASY = ["Fetch the page title of https://example.com/about", "List the files in /tmp/run-1119/out",
        "Look up the CNPJ of WeWork Faria Lima 4055", "Count the rows in /tmp/run-1119/out/ledger.csv",
        "Grep /tmp/run-1119/logs for TimeoutError"]
SENSITIVE = ["Draft the reply email to Figure about invoice 1023", "Fix the failing test in tools/delegate_tool.py",
             "Check the SOW terms Jackson sent",
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
# A sensitive word joined to another by a hyphen or dash is still that word (round-2 review, 19 Nov 2026: the
# boundaries skipped both sides of a hyphen, so these ran at medium, or kept an explicit low).
HYPHENATED = [
    "Read the wire-transfer instructions", "Read the contract-signing checklist", "Re-send it to Theo",
    "Read the research-synthesis brief", "Read the code-review checklist", "Read invoice-1023",
    "Read the wire\u2013transfer instructions", "Re-write the README in /tmp/run-1119",
]
# `get` / `show` are light verbs, not read-only ones: a goal that also names an access grant, a send to someone,
# a destructive op or a restart never runs below high, and an explicit low is floored (round-2 review N1, 19 Nov 2026).
LIGHT_VERBS = [
    "Get rid of the old backups", "Get rid of the duplicate rows in the contributors table",
    "Get Loic access to the AWS console", "Get Dani admin access to Mercury",
    "Get everyone on the team onboarded to 1Password", "Get the deck to Armin", "Get Theo the deck",
    "Get the contributors their Scout codes", "Show Jackson the dashboard", "Get the server back up",
    "Show the payout and send it", "Give Maxence edit permissions on the finance folder",
]
# A fixed read-only command is a deterministic step (Tim, 19 Nov 2026: "low only for fetching abc, deterministic").
RUN_READ_ONLY = ["Run `git log -5 --oneline` in /tmp/run-1119/checkout", "Run `df -h`", "Run `wc -l /tmp/run-1119/a.csv`"]
# Commands that change state, chain, or read secrets are not.
RUN_NOT_READ_ONLY = ["Run `git push origin main`", "Run `ls /tmp/x; rm -rf /tmp/x`", "Run `cat secrets.yaml`",
                     "Run `find /tmp/run-1119 -delete`", "Run `sed -i s/a/b/ /tmp/run-1119/a.txt`"]
# Deep work runs at xhigh whoever asked less: reviews, audits, QA, fact-checks, plans, debugging, synthesis.
DEEP = [
    ("Write an independent adversarial review of the Series A deck outline", "review"),
    ("Audit the money figures in /tmp/run-1119/plan.md", "review"),
    ("Verify the round-1 fixes actually hold and find new problems", "review"),
    ("E2E functional QA of the desktop flows on the candidate build", "review"),
    ("Re-score these sales leads from the latest evidence", "review"),
    ("Evaluate whether the cache layer is safe under concurrent writers", "review"),
    ("Plan the migration of the sessions table to the new store", "planning"),
    ("Draft a rollout plan for the phone line", "planning"),
    ("Design the architecture for the payout watcher", "planning"),
    ("Debug the flaky launch-wave test", "debugging"),
    ("Find the root cause of the 429s on wide launches", "debugging"),
    ("Investigate why the nightly update fails on the Mac", "debugging"),
    ("Research and synthesize what labs pay for egocentric video", "synthesis"),
    ("Research and write a cited comparison of LiveKit and Pipecat", "synthesis"),
    ("Recommend which GPU cloud to use for the VLA runs", "synthesis"),
    ("Revisar o contrato e apontar os riscos", "review"),
]
# Not deep: a plain read of a review, a word used as a description, or a topic named late in a long brief.
NOT_DEEP = [
    "Read the review notes in /tmp/run-1119/review.md",
    "Research GPU vendors Hub is buying or evaluating and write findings to /tmp/run-1119/gpu.md",
    "Research what the Hermes community recommends for heavy multi-agent users",
    "Lane A1: how commercial agent products architect the backend that serves a fast UI",
    "Research what power users say about swarms: how many parallel subagents, effort per subagent, budgets, "
    "orchestrator-worker patterns, worker sizing, launch pacing, observability, and finally cross-model review",
]
# One bounded read-only step without a concrete target: medium at most, never automatic.
BOUNDED = ["Search the web for the street address of Figure AI headquarters", "List competitors of Hub"]
# Read-only but not ONE mechanical operation, or not recognisably English: never auto-lowered.
NOT_MECHANICAL = [
    "Get the README in /tmp/run-1119", "Show the last 20 lines of /tmp/run-1119/log.txt",
    "List the files in /tmp/run-1119/out and count them", "Fetch the page title, then the meta description",
    "Find which of the three vendors is cheapest", "Liste os arquivos em /tmp/run-1119/out",
    "List les fichiers dans /tmp/run-1119/out", "ファイルの一覧を表示して",
    "Rename notes.md to notes-final.md in the run dir", "Format the JSON in /tmp/run-1119/out.json",
]


def test_omitted_effort_easy_mechanical_goals_run_medium_and_say_why(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": g} for g in EASY + RUN_READ_ONLY]) == [
        ("medium", "effort medium (auto: deterministic)")] * len(EASY + RUN_READ_ONLY)


@pytest.mark.parametrize("goal", NOT_MECHANICAL)
def test_compound_write_or_unrecognised_goals_keep_the_default(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": goal}])[0][0] == "high"


@pytest.mark.parametrize("goal", SENSITIVE + REVIEWED + MULTILINGUAL + HYPHENATED + LIGHT_VERBS)
def test_money_legal_sends_code_writes_and_research_never_run_below_high(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    [(effort, label)] = _routes([{"goal": goal}])
    assert effort == "high" and label == "effort high (default)"
    for asked in ("low", "medium"):  # an explicit cheap pick is floored too
        [(effort, label)] = _routes([{"goal": goal, "effort": asked}])
        assert effort == "high" and label.startswith(f"effort high (floor: asked {asked}")


@pytest.mark.parametrize("goal", REVIEWED[:4] + HYPHENATED[:3])
def test_a_sensitive_context_floors_an_explicit_pick_on_a_neutral_goal(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    task = {"goal": "Fetch the page title of https://example.com/about", "context": goal, "effort": "low"}
    assert _routes([task])[0][0] == "high"


def test_explicit_picks_win_on_neutral_tasks_and_xhigh_is_never_automatic_for_them(home):
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


@pytest.mark.parametrize("default", ["", None])
def test_the_floor_only_raises_an_inherited_level(home, default):
    """An empty default inherits the parent's level: sensitive work gets max(high, parent), never less than
    neutral work gets (round-2 review N2, 19 Nov 2026: an xhigh parent's send ran at high, its neutral work at xhigh)."""
    from tools.delegate_tool_config import _child_reasoning, _load_config
    home({"backend": "effort-router", "reasoning_effort": default})
    tasks = [{"goal": "Send the Slack update to Theo"}, {"goal": "Send the Slack update to Theo", "effort": "low"},
             {"goal": "Think through the three options for the onboarding flow and pick one"}]
    for level, sensitive in (("xhigh", "xhigh"), ("high", "high"), ("medium", "high"), ("low", "high")):
        parent = _parent(reasoning_config={"enabled": True, "effort": level})
        children = [_child_reasoning(parent, _load_config(), effort) for effort, _ in _routes(tasks, parent=parent)]
        # an explicit low is the parent's pick, so the floor lifts it to high, never to the parent's level
        assert children == [{"enabled": True, "effort": e} for e in (sensitive, "high", level)], level
    disabled = _parent(reasoning_config={"enabled": False})
    [(effort, label)] = _routes(tasks[:1], parent=disabled)
    assert effort == "high" and label.startswith("effort high (floor: default")
    assert _child_reasoning(disabled, _load_config(), effort) == {"enabled": True, "effort": "high"}


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
        "effort medium (auto: deterministic)", "effort high (default)", "effort low"]
    assert out["effort_mix"] == {"medium": 1, "high": 1, "low": 1}


@pytest.mark.parametrize("goal,kind", DEEP)
def test_reviews_plans_debugging_and_synthesis_run_at_xhigh_whoever_asked_less(home, goal, kind):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": goal}]) == [("xhigh", f"effort xhigh (deep: default high, {kind})")]
    for asked in ("low", "medium", "high"):
        assert _routes([{"goal": goal, "effort": asked}]) == [("xhigh", f"effort xhigh (deep: asked {asked}, {kind})")]


@pytest.mark.parametrize("goal", NOT_DEEP)
def test_a_read_a_description_or_a_late_topic_is_not_deep_work(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    assert _routes([{"goal": goal}])[0][0] == "high"


def test_low_is_only_for_deterministic_steps_and_medium_only_for_bounded_ones(home):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    for goal in EASY + RUN_READ_ONLY:  # deterministic: an explicit low stands
        assert _routes([{"goal": goal, "effort": "low"}]) == [("low", "effort low")]
    for goal in BOUNDED:  # bounded but no concrete target: never automatic, low becomes medium, medium stands
        assert _routes([{"goal": goal}]) == [("high", "effort high (default)")]
        assert _routes([{"goal": goal, "effort": "low"}]) == [
            ("medium", "effort medium (raised: asked low, not deterministic)")]
        assert _routes([{"goal": goal, "effort": "medium"}]) == [("medium", "effort medium")]
    goal = "Think through the three options for the onboarding flow and pick one"  # neither: a cheap pick is raised
    assert _routes([{"goal": goal, "effort": "low"}, {"goal": goal, "effort": "medium"}]) == [
        ("high", "effort high (raised: asked low, not deterministic)"),
        ("high", "effort high (raised: asked medium, not one bounded read-only step)")]


@pytest.mark.parametrize("goal", RUN_NOT_READ_ONLY)
def test_a_command_that_writes_chains_or_reads_secrets_is_never_cheap(home, goal):
    home({"backend": "effort-router", "reasoning_effort": "high"})
    for asked in (None, "low", "medium"):
        assert _routes([{"goal": goal, **({"effort": asked} if asked else {})}])[0][0] == "high"


OPTIONS = [
    {"name": "gpt", "description": "OpenAI GPT-6.2 Astra; independent reviewer.", "base_url": "https://api.openai.com/v1",
     "api_key": "sk-test-gpt", "model": "gpt-6.2-astra", "fallback_providers": []},
    {"name": "gemini", "description": "Google Gemini 4 Neon; claim checker.", "base_url": "https://vertex.example/v1",
     "api_key": "test-gemini", "model": "google/gemini-4-neon", "fallback_providers": [], "reasoning_effort": "medium"},
    {"name": "broken", "description": "no model"},
]


def test_options_add_an_option_param_with_their_descriptions_and_nothing_without_them(home):
    from tools.registry import registry
    home({"backend": "effort-router", "reasoning_effort": "high"})
    props = registry.get_definitions({"delegate_task"})[0]["function"]["parameters"]["properties"]["tasks"]["items"]
    assert "option" not in props["properties"]
    home({"backend": "effort-router", "reasoning_effort": "high", "backends": {"effort-router": {"options": OPTIONS}}})
    definition = registry.get_definitions({"delegate_task"})[0]["function"]
    option = definition["parameters"]["properties"]["tasks"]["items"]["properties"]["option"]
    assert option["enum"] == ["gpt", "gemini"]  # the malformed entry is skipped
    assert "gpt: OpenAI GPT-6.2 Astra" in option["description"] and "Gemini 4 Neon" in option["description"]
    assert "adversarial or multi-model review" in definition["description"]


def test_an_option_routes_its_own_model_with_the_same_effort_tiers(home):
    from agent.delegation_registry import get_active_provider
    from tools.delegate_tool_config import _load_config
    home({"backend": "effort-router", "reasoning_effort": "high", "provider": "anthropic", "model": "claude-opus-5-5",
          "request_overrides": {"service_tier": "priority"},
          "backends": {"effort-router": {"options": [*OPTIONS[:2],
                                                     {"name": "astra", "description": "GPT via the API key.",
                                                      "provider": "openai-api", "model": "gpt-6.2-astra"}]}}})
    config, provider = _load_config(), get_active_provider(_load_config())
    review = "Write an independent adversarial review of the Series A deck outline"
    route = provider.route_task({"goal": review, "option": "astra", "inherited_effort": "high"}, config)
    assert (route["provider"], route["model"], route["reasoning_effort"]) == ("openai-api", "gpt-6.2-astra", "xhigh")
    assert "request_overrides" not in route and "base_url" not in route  # the default model's knobs stay behind
    assert route["label"] == "astra · effort xhigh (deep: default high, review)"
    # The option's own default level stands for neutral work, under the same tiers.
    neutral = provider.route_task({"goal": "Think through the onboarding options", "option": "gemini"}, config)
    assert (neutral["model"], neutral["reasoning_effort"], neutral["fallback_providers"]) == (
        "google/gemini-4-neon", "medium", [])
    # No option: the default route, untouched.
    plain = provider.route_task({"goal": review}, config)
    assert (plain["provider"], plain["model"], plain["reasoning_effort"]) == ("anthropic", "claude-opus-5-5", "xhigh")


def test_an_adversarial_review_fans_one_brief_out_to_three_models_through_delegate_task(home, recorded):
    """The multi-model recipe end to end through core: one brief, three options, each child built on its own
    model and endpoint with no fallback, every reviewer at xhigh, and each result names the model it ran on."""
    home({"backend": "effort-router", "reasoning_effort": "high",
          "backends": {"effort-router": {"options": OPTIONS[:2]}}})
    from tools.delegate_tool import delegate_task
    brief = {"goal": "Write an independent adversarial review of the claims in /tmp/run-1119/REPORT.md",
             "context": "Work blind: findings only, each with evidence."}
    out = json.loads(delegate_task(tasks=[{**brief}, {**brief, "option": "gpt"}, {**brief, "option": "gemini"}],
                                   parent_agent=_parent()))
    assert [r["status"] for r in out["results"]] == ["completed"] * 3
    # The plain task inherits the parent's model (no pin); the options pin theirs.
    assert [(b["model"], b["override_base_url"], b["override_reasoning_effort"]) for b in recorded] == [
        (None, None, "xhigh"), ("gpt-6.2-astra", "https://api.openai.com/v1", "xhigh"),
        ("google/gemini-4-neon", "https://vertex.example/v1", "xhigh")]
    assert [b["routing_cfg"].get("fallback_providers") for b in recorded[1:]] == [[], []]
    assert [r["route"]["label"] for r in out["results"]] == [
        "effort xhigh (deep: default high, review)", "gpt · effort xhigh (deep: default high, review)",
        "gemini · effort xhigh (deep: default medium, review)"]  # its own medium default, raised like any other
    assert [r["route"]["model"] for r in out["results"]][1:] == ["gpt-6.2-astra", "google/gemini-4-neon"]
    assert out["effort_mix"] == {"xhigh": 3}
