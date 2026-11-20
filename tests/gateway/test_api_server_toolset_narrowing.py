"""Authenticated per-request toolset narrowing (``X-Hermes-Toolsets``).

An authenticated caller may NARROW the toolsets of the agent that serves its request, never
widen them: the effective set is the requested names intersected with the ``api_server``
platform toolsets the agent would have had anyway. ``none`` means no tools at all. The
effective set is echoed in ``X-Hermes-Toolsets-Applied``; without an honoured header the
response carries no such header and the agent is built exactly as before.
"""

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware, security_headers_middleware

API_KEY = "sk-narrowing-test-key-0123456789"
SERVER_TOOLSETS = {"file", "myserver", "terminal", "web"}
FULL = sorted(SERVER_TOOLSETS)


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


@pytest.fixture
def built(monkeypatch):
    """Real ``_run_agent`` / ``_create_agent``; only the AIAgent class and runtime are faked.
    Returns the list of ``enabled_toolsets`` each constructed agent received."""
    toolsets_seen: list = []

    def _fake_agent(**kwargs):
        toolsets_seen.append(kwargs["enabled_toolsets"])
        agent = MagicMock()
        agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

        def _run(**_kw):
            cb = kwargs.get("stream_delta_callback")
            if cb is not None:
                cb("hi")
            return {"final_response": "hi", "messages": [], "api_calls": 1}

        agent.run_conversation.side_effect = _run
        return agent

    monkeypatch.setattr("run_agent.AIAgent", _fake_agent)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "openrouter", "api_key": "sk-x", "base_url": "https://openrouter.ai/api/v1",
        "api_mode": "chat_completions"})
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "global/model")
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {}))
    monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_a, **_k: set(SERVER_TOOLSETS))
    return toolsets_seen


async def _post(cli, *, toolsets=None, key=API_KEY, stream=False, session_id=None):
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if toolsets is not None:
        headers["X-Hermes-Toolsets"] = toolsets
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id
    resp = await cli.post("/v1/chat/completions", headers=headers, json={
        "messages": [{"role": "user", "content": "hello"}], "stream": stream})
    await resp.read()
    assert resp.status == 200, await resp.text()
    return resp


def _patched_store(adapter):
    return patch.object(adapter, "_ensure_store", lambda: None)


class TestHeaderHonouredOnlyWhenAuthenticated:
    @pytest.mark.asyncio
    async def test_no_api_key_configured_ignores_header(self, built):
        adapter = _make_adapter()
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await _post(cli, toolsets="web", key=None)
        assert "X-Hermes-Toolsets-Applied" not in resp.headers
        assert built == [FULL]

    @pytest.mark.asyncio
    async def test_wrong_key_is_rejected_before_any_agent(self, built):
        adapter = _make_adapter(API_KEY)
        async with TestClient(TestServer(_create_app(adapter))) as cli:
            resp = await cli.post("/v1/chat/completions", headers={
                "Authorization": "Bearer wrong", "X-Hermes-Toolsets": "web"},
                json={"messages": [{"role": "user", "content": "hello"}]})
        assert resp.status == 401
        assert built == []


class TestNarrowing:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_authenticated_header_narrows(self, built, stream):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await _post(cli, toolsets=" web ", stream=stream)
        assert resp.headers["X-Hermes-Toolsets-Applied"] == "web"
        assert built == [["web"]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_widening_is_ignored(self, built, stream):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                mixed = await _post(cli, toolsets="web,browser,otherserver,myserver", stream=stream)
                only_foreign = await _post(cli, toolsets="browser,code_execution", stream=stream)
        assert mixed.headers["X-Hermes-Toolsets-Applied"] == "myserver,web"
        assert only_foreign.headers["X-Hermes-Toolsets-Applied"] == "none"
        assert built == [["myserver", "web"], []]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_none_means_zero_toolsets(self, built, stream):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await _post(cli, toolsets="none", stream=stream)
        assert resp.headers["X-Hermes-Toolsets-Applied"] == "none"
        assert built == [[]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_no_header_is_unchanged(self, built, stream):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await _post(cli, stream=stream)
        assert "X-Hermes-Toolsets-Applied" not in resp.headers
        assert built == [FULL]

    @pytest.mark.asyncio
    async def test_same_session_never_reuses_a_wider_or_narrower_agent(self, built):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                narrowed = await _post(cli, toolsets="web", session_id="call-1")
                normal = await _post(cli, session_id="call-1")
                narrowed_again = await _post(cli, toolsets="none", session_id="call-1")
        assert narrowed.headers["X-Hermes-Toolsets-Applied"] == "web"
        assert "X-Hermes-Toolsets-Applied" not in normal.headers
        assert narrowed_again.headers["X-Hermes-Toolsets-Applied"] == "none"
        assert built == [["web"], FULL, []]


class TestAgentConstruction:
    def test_create_agent_only_intersects(self, built):
        """A narrowing list naming toolsets the server does not enable never adds them."""
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            adapter._create_agent(session_id="s1", toolsets_narrowing=["web", "browser"])
        assert built == [["web"]]

    def test_empty_selection_builds_an_agent_with_no_tools(self):
        """``enabled_toolsets=[]`` (what ``none`` produces) is "no toolsets", not "all": the real
        AIAgent carries zero tool definitions, MCP and memory-provider tools included."""
        from run_agent import AIAgent
        from toolsets import resolve_toolset
        common = dict(model="test/model", api_key="sk-test", base_url="http://127.0.0.1:9/v1",
                      provider="openrouter", api_mode="chat_completions", quiet_mode=True,
                      skip_memory=True, skip_context_files=True, platform="api_server")
        empty = AIAgent(enabled_toolsets=[], **common)
        assert empty.tools == []
        assert empty.valid_tool_names == set()
        narrowed = AIAgent(enabled_toolsets=["file"], **common)
        assert narrowed.valid_tool_names
        assert narrowed.valid_tool_names <= set(resolve_toolset("file"))

    def test_cached_agent_signature_separates_no_tools_from_all_tools(self):
        """The gateway agent cache (session-authority path) keys on the toolset selection: a
        zero-toolset turn must never reuse an agent built with ``None`` (= every toolset)."""
        from gateway.run import GatewayRunner
        sig = GatewayRunner._agent_config_signature
        runtime = {"api_key": "k", "base_url": "u", "provider": "p"}
        assert sig("m", runtime, [], "") != sig("m", runtime, None, "")
        assert sig("m", runtime, ["web"], "") != sig("m", runtime, FULL, "")


class TestSessionAuthorityTurn:
    """Under session authority the API turn runs through TurnRunner and its cached agents: the
    admission's ``toolsets_narrowing`` setting narrows ``ctx.enabled_toolsets`` (the agent cache
    signature input), so a narrowed turn and a normal turn never share an agent."""

    def _with_settings(self, settings, enabled):
        from gateway.session_api_turn import api_execution, api_turn_toolsets
        token = api_execution.set({"settings": settings})
        try:
            return api_turn_toolsets(enabled)
        finally:
            api_execution.reset(token)

    def test_narrowed_admission_intersects(self):
        assert self._with_settings({"toolsets_narrowing": ["web", "browser"]}, FULL) == ["web"]
        assert self._with_settings({"toolsets_narrowing": []}, FULL) == []

    def test_normal_admission_and_non_api_turns_unchanged(self):
        from gateway.session_api_turn import api_turn_toolsets
        assert self._with_settings({"requested_model": None}, FULL) == FULL
        assert api_turn_toolsets(FULL) == FULL


# -- phone line third-party calls: a narrowed agent never sees Tim's memory, profile or context files --

MARKERS = {"memory": "MARKER_MEMORY_N3", "user": "MARKER_USER_N3", "soul": "MARKER_SOUL_N3",
           "agents": "MARKER_AGENTS_N3"}
ISOLATED = {"skip_memory": True, "skip_context_files": True, "load_soul_identity": False}


@pytest.fixture
def built_kwargs(built, monkeypatch):
    """Every kwargs dict the (fake) AIAgent was constructed with, on top of ``built``."""
    import run_agent
    inner, seen = run_agent.AIAgent, []

    def _record(**kwargs):
        seen.append(kwargs)
        return inner(**kwargs)

    monkeypatch.setattr("run_agent.AIAgent", _record)
    return seen


class TestNarrowedAgentIsolationClassicPath:
    """``_run_agent`` -> ``_create_agent``: an honoured narrowing (``none`` included) builds an agent
    with no memory, no user profile, no SOUL.md and no context files; no narrowing builds it as before."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("toolsets", ["none", "web"])
    async def test_narrowed_request_skips_memory_and_context_files(self, built_kwargs, toolsets):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                await _post(cli, toolsets=toolsets, session_id="call-n3")
        (kwargs,) = built_kwargs
        assert {k: kwargs.get(k) for k in ISOLATED} == ISOLATED
        assert kwargs["memory_manager"] is None

    @pytest.mark.asyncio
    async def test_unnarrowed_request_builds_exactly_as_before(self, built_kwargs):
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                await _post(cli, session_id="call-n3")
        (kwargs,) = built_kwargs
        assert not set(ISOLATED) & set(kwargs)

    def test_narrowed_agent_never_takes_the_sessions_parked_memory_manager(self, built_kwargs):
        """The per-session MemoryManager parked by an earlier normal turn stays parked: a narrowed
        turn neither receives it nor checks it out, and the next normal turn still gets it."""
        adapter = _make_adapter(API_KEY)
        parked = object()
        checkout = MagicMock(return_value=parked)
        adapter._memory_sessions.checkout = checkout
        with _patched_store(adapter):
            adapter._create_agent(session_id="s1", toolsets_narrowing=[])
            adapter._create_agent(session_id="s1", toolsets_narrowing=["web"])
            assert checkout.call_count == 0
            adapter._create_agent(session_id="s1")
        assert [kw["memory_manager"] for kw in built_kwargs] == [None, None, parked]
        checkout.assert_called_once_with("s1")


def _write_tim_files(home, cwd):
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "memories" / "MEMORY.md").write_text(MARKERS["memory"] + " Tim's card ends 0000\n")
    (home / "memories" / "USER.md").write_text(MARKERS["user"] + " Tim lives at a private address\n")
    (home / "SOUL.md").write_text(MARKERS["soul"] + " You are Tim's assistant.\n")
    (cwd / "AGENTS.md").write_text(MARKERS["agents"] + " project rules\n")


class TestNarrowedRealAgentPrompt:
    """A REAL AIAgent built by ``_create_agent`` (fake credentials, no network) in a home holding
    MEMORY.md, USER.md, SOUL.md and an AGENTS.md: narrowed, the prompt carries none of them;
    un-narrowed (same home) it carries all of them, so the setup really loads them."""

    def _prompt(self, monkeypatch, tmp_path, narrowing):
        from hermes_constants import get_hermes_home
        home, cwd = get_hermes_home(), tmp_path / "work"
        cwd.mkdir(exist_ok=True)
        _write_tim_files(home, cwd)
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("TERMINAL_CWD", str(cwd))
        monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
            "provider": "openrouter", "api_key": "sk-test-fake", "base_url": "http://127.0.0.1:9/v1",
            "api_mode": "chat_completions"})
        monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test/model")
        monkeypatch.setattr("gateway.run._load_gateway_config", dict)
        monkeypatch.setattr("gateway.run.GatewayRunner._load_reasoning_config", staticmethod(lambda model="": {}))
        monkeypatch.setattr("gateway.run.GatewayRunner._load_fallback_model", staticmethod(lambda: None))
        monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 5)
        monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *_a, **_k: {"file"})
        adapter = _make_adapter(API_KEY)
        with _patched_store(adapter):
            agent = adapter._create_agent(session_id="n3-real", toolsets_narrowing=narrowing)
        return agent, agent._build_system_prompt()

    @pytest.mark.parametrize("narrowing", [[], ["file"]], ids=["none", "file"])
    def test_narrowed_prompt_has_no_memory_user_soul_or_context_files(self, monkeypatch, tmp_path, narrowing):
        agent, prompt = self._prompt(monkeypatch, tmp_path, narrowing)
        assert [m for m in MARKERS.values() if m in prompt] == []
        assert agent._memory_store is None and agent._memory_manager is None

    def test_unnarrowed_prompt_still_loads_them(self, monkeypatch, tmp_path):
        _agent, prompt = self._prompt(monkeypatch, tmp_path, None)
        assert [m for m in MARKERS.values() if m not in prompt] == []


class TestNarrowedAgentIsolationTurnRunner:
    """Session-authority path: TurnRunner builds (and caches) the agent. A narrowed admission builds
    it isolated, and its cache signature never matches an un-narrowed agent's (or the reverse)."""

    def _resolve(self, monkeypatch, settings, user_config=None):
        from types import SimpleNamespace
        from gateway import session_policy
        from gateway.run import GatewayRunner
        from gateway.session_api_turn import api_execution
        from gateway.session_execution import GatewaySessionAgentMixin
        monkeypatch.setattr(session_policy, "policy_for_source", lambda runner, source: None)
        built, cache = [], {}
        turn = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
        turn._runner = SimpleNamespace(
            _agent_config_signature=GatewayRunner._agent_config_signature,
            _extract_cache_busting_config=lambda cfg: {}, _agent_cache_lock=__import__("threading").Lock(),
            _agent_cache=cache, _enforce_agent_cache_cap=lambda: None, _prefill_messages=None,
            _refresh_fallback_model=lambda: None, persistence=None)
        source = SimpleNamespace(chat_id="c", user_id="u", user_id_alt=None, user_name=None, chat_name=None,
                                 chat_type="dm", thread_id=None)
        turn._ctx = SimpleNamespace(
            source=source, user_config=user_config or {}, enabled_toolsets=["web"], disabled_toolsets=None,
            session_id="s", session_key="k", interim_assistant_messages_enabled=True,
            AIAgent=lambda **kw: built.append(kw) or SimpleNamespace())
        turn._cached_sid_is_dead = lambda lock, cache: (None, False)
        turn._current_message_count = lambda: 0
        turn._lookup_cached_agent = lambda *a, **k: GatewaySessionAgentMixin._CachedAgentLookup()
        token = api_execution.set({"settings": settings})
        try:
            turn._resolve_turn_agent({"model": "m", "runtime": {"provider": "p"}}, "api_server", None, 5, None, {})
        finally:
            api_execution.reset(token)
        (kwargs,) = built
        return kwargs, cache["k"][1]

    @pytest.mark.parametrize("narrowing", [[], ["web"]], ids=["none", "web"])
    def test_narrowed_admission_builds_isolated_agent_with_its_own_signature(self, monkeypatch, narrowing):
        narrowed, narrowed_sig = self._resolve(monkeypatch, {"toolsets_narrowing": narrowing})
        normal, normal_sig = self._resolve(monkeypatch, {"requested_model": None})
        assert {k: narrowed[k] for k in ISOLATED} == ISOLATED
        assert normal["skip_memory"] is False and normal["skip_context_files"] is False
        assert normal["load_soul_identity"] is True
        assert narrowed_sig != normal_sig

    def test_signature_differs_even_when_context_files_are_already_skipped(self, monkeypatch):
        """A profile with ``gateway.platforms.api_server.skip_context_files`` still loads memory
        un-narrowed: the signature keys skip_memory too, so the agents never swap."""
        cfg = {"gateway": {"platforms": {"api_server": {"skip_context_files": True}}}}
        narrowed, narrowed_sig = self._resolve(monkeypatch, {"toolsets_narrowing": ["web"]}, cfg)
        normal, normal_sig = self._resolve(monkeypatch, {"requested_model": None}, cfg)
        assert narrowed["skip_memory"] is True and normal["skip_memory"] is False
        assert narrowed_sig != normal_sig

    def test_signature_unchanged_for_unnarrowed_agents(self):
        from gateway.run import GatewayRunner
        sig = GatewayRunner._agent_config_signature
        runtime = {"api_key": "k", "base_url": "u", "provider": "p"}
        assert sig("m", runtime, FULL, "", skip_memory=False) == sig("m", runtime, FULL, "")
        assert sig("m", runtime, FULL, "", skip_memory=True) != sig("m", runtime, FULL, "")
