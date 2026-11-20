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
