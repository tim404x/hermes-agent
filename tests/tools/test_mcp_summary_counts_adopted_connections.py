"""The discovery summary counts a server a profile ADOPTED from another profile's live connection
as connected (K15, Tim's install, 20 Nov 2026).

Under a multiplexer (dashboard / desktop backend serving several profiles) a profile whose
``gbrain`` route and bearer equal the launch profile's adopts that live connection instead of
opening its own. The adopted connection stays keyed under its owner, and ``_connected_summary``
looked only at the adopting scope's own key, so every such build logged
``MCP: ... (3 failed: heyreach (not attempted (in retry cooldown)); gbrain (...); hub_ego (...))``
while the profile's sessions were calling ``mcp__gbrain__*``. Weeks of that line read as "the bots
lost the brain" and drove a false monitoring alarm. A failure reason is now read from the ledgers,
never assumed.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from hermes_constants import hermes_home_key, reset_hermes_home_override, set_hermes_home_override


def _server(name, cfg):
    from tools.mcp_tool_registration import _adopter_identity_digest
    tool = SimpleNamespace(name="t", description="d", inputSchema={"type": "object", "properties": {}},
                           annotations=None)
    return SimpleNamespace(name=name, session=object(), _config=cfg, _tools=[tool], tool_timeout=30,
                           initialize_result=None, _registered_tool_names=[], _sampling=None,
                           _resolved_identity=_adopter_identity_digest(name, cfg))


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Multiplex on, clean MCP ledgers, a scope switcher for homes A and B; restores everything."""
    import tools.mcp_tool as core
    from tools import mcp_tool_config as _config
    from tools.registry import registry

    homes = {k: tmp_path / "profiles" / k for k in ("a", "b")}
    for home in homes.values():
        home.mkdir(parents=True)
    monkeypatch.setattr("agent.secret_scope.is_multiplex_active", lambda: True)
    monkeypatch.setattr(core, "_ensure_mcp_sdk", lambda: True)
    monkeypatch.setattr(_config, "_filter_suspicious_mcp_servers", lambda servers: servers)
    ledgers = ("_servers", "_server_scope_keys", "_server_tool_scopes", "_server_connecting",
               "_server_connect_errors", "_server_connect_retry_after", "_server_connect_failures",
               "_server_error_counts", "_server_breaker_opened_at", "_lazy_server_configs",
               "_mcp_tool_server_names", "_orphaned_adopters", "_parallel_safe_servers",
               "_server_trust_levels", "_tool_read_only_hints")
    saved = {n: type(getattr(core, n))(getattr(core, n)) for n in ledgers}
    for n in ledgers:
        getattr(core, n).clear()
    tokens = []

    def enter(which):
        tokens.append(set_hermes_home_override(homes[which]))
        return hermes_home_key(homes[which])

    yield enter
    for tool_name in list(registry.get_tool_names_for_toolset("mcp-brain")):
        for home in homes.values():
            registry.deregister(tool_name, scope=hermes_home_key(home))
    for token in reversed(tokens):
        reset_hermes_home_override(token)
    for n in ledgers:
        getattr(core, n).clear()
        getattr(core, n).update(saved[n])


def _summaries(caplog):
    return [r.getMessage() for r in caplog.records if "tool(s) from" in r.getMessage()]


def test_adopted_shared_connection_is_reported_connected(two_profiles, monkeypatch, caplog):
    """Production path: profile B's ``discover_mcp_tools`` adopts A's live ``brain`` connection
    (same URL and bearer). B gets the tools without a connect, and the summary names no failure."""
    from tools import mcp_tool_config as _config
    from tools import mcp_tool_discovery as disc
    from tools import mcp_tool_registration as reg
    from tools.registry import registry

    cfg = {"url": "https://brain.example/mcp", "headers": {"Authorization": "Bearer shared"}}
    monkeypatch.setattr(_config, "_load_mcp_config", lambda: {"brain": dict(cfg)})
    monkeypatch.setattr(disc, "_acquire_discovery_lock_with_retry", lambda: None)

    def no_connect(new_servers):
        raise AssertionError(f"adopted server must not be reconnected: {sorted(new_servers)}")

    monkeypatch.setattr(disc, "_run_discovery_pass", no_connect)

    two_profiles("a")
    owner = _server("brain", dict(cfg))
    disc._adopt_server("brain", owner)
    owner._registered_tool_names = reg._register_server_tools("brain", owner, dict(cfg))

    two_profiles("b")
    assert registry.get_tool_names_for_toolset("mcp-brain") == []
    with caplog.at_level(logging.INFO, logger="tools.mcp_tool"):
        tool_names = disc.discover_mcp_tools()

    assert "mcp__brain__t" in tool_names
    assert registry.get_tool_names_for_toolset("mcp-brain") == ["mcp__brain__t"]
    assert disc.mcp_servers_ready() == ["brain"]
    summaries = _summaries(caplog)
    assert summaries == ["  MCP: 1 tool(s) from 1 server(s)"], summaries


def test_unconnected_reason_reads_the_ledgers(two_profiles, caplog):
    """No recorded connect error: the reason names the real state (cooldown, still connecting,
    nothing recorded) instead of always claiming a retry cooldown; a recorded error wins."""
    import tools.mcp_tool as core
    from tools import mcp_tool_discovery as disc
    from tools.mcp_tool_scope import _server_key

    two_profiles("a")
    with core._lock:
        core._server_connect_retry_after[_server_key("cooled")] = time.monotonic() + 60
        core._server_connecting.add(_server_key("pending"))
    disc._note_connect_failure("broken", RuntimeError("401 Unauthorized"))

    _, connected, failed = disc._connected_summary(["cooled", "pending", "ghost", "broken"])
    reasons = dict(failed)
    assert connected == 0
    assert reasons["cooled"] == "not attempted (in retry cooldown)"
    assert reasons["pending"] == "still connecting"
    assert reasons["ghost"] == "not connected (no connect attempt recorded)"
    assert "401 Unauthorized" in reasons["broken"]
