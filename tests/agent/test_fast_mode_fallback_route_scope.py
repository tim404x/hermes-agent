"""A pinned fast param never follows the agent onto a route that does not take it.

The static ``priority`` tier pins the BUILD route's fast param into ``agent.request_overrides``:
``speed: "fast"`` for an Anthropic primary. A fallback (or a ``/model`` switch) swaps model,
provider and client in place but keeps that dict, so the Anthropic-only ``speed`` reached the
fallback's request builder. Every non-Anthropic builder splats ``request_overrides`` into the
SDK kwargs: Codex Responses preflight refused it (``unsupported field(s): speed``, a
non-retryable client error) and the fallback died at the moment the primary was down
(13 Nov 2026: Anthropic 503 -> openai-api gpt-6-astra, seven turns lost on three profiles).

``speed`` has one consumer, ``_build_anthropic_kwargs`` (a flag the Anthropic adapter gates per
model and endpoint itself), so the single place the overrides are read
(``effective_request_overrides``) hands it to that builder only.
"""

from types import SimpleNamespace

import pytest

from agent import fast_mode
from agent.codex_responses_adapter import _preflight_codex_api_kwargs

OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com"


def _anthropic_primary(**kw):
    """Gateway/CLI shape: static tier, the Anthropic fast param pinned at build time."""
    base = dict(
        service_tier="priority",
        model="claude-opus-5",
        provider="anthropic",
        base_url=ANTHROPIC_URL,
        _anthropic_base_url=ANTHROPIC_URL,
        api_mode="anthropic_messages",
        request_overrides={"speed": "fast"},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _after_fallback(agent, *, model, provider, base_url, api_mode):
    """What ``try_activate_fallback`` leaves: route fields swapped, ``request_overrides`` kept."""
    agent.model, agent.provider, agent.base_url, agent.api_mode = model, provider, base_url, api_mode
    return agent


def test_anthropic_primary_keeps_its_speed_param():
    agent = _anthropic_primary()
    assert fast_mode.effective_request_overrides(agent) == {"speed": "fast"}


def test_speed_does_not_follow_the_agent_onto_an_openai_responses_fallback():
    agent = _after_fallback(_anthropic_primary(), model="gpt-6-astra", provider="openai-api",
                            base_url=OPENAI_URL, api_mode="codex_responses")
    overrides = fast_mode.effective_request_overrides(agent)
    assert "speed" not in overrides
    # The fallback route's own fast param replaces it: the turn stays fast where fast is sold.
    assert overrides == {"service_tier": "priority"}
    # The pin itself is untouched, so restoring the primary restores its fast mode.
    assert agent.request_overrides == {"speed": "fast"}

    # The contract that killed the fallback: Responses preflight refuses unknown top-level fields.
    request = {"model": "gpt-6-astra", "instructions": "x", "input": [], "store": False}
    with pytest.raises(ValueError, match="unsupported field"):
        _preflight_codex_api_kwargs({**request, "speed": "fast"})
    assert _preflight_codex_api_kwargs({**request, **overrides})["service_tier"] == "priority"


def test_speed_does_not_reach_a_proxy_or_chat_completions_fallback():
    # No fast route at all (a proxy): nothing fast may reach it, the stale pin included.
    agent = _after_fallback(_anthropic_primary(), model="anthropic/claude-opus-5", provider="openrouter",
                            base_url="https://openrouter.ai/api/v1", api_mode="chat_completions")
    assert "speed" not in fast_mode.effective_request_overrides(agent)


def test_anthropic_messages_route_still_receives_the_flag_after_a_model_switch():
    # Same builder, another Claude: the flag stays (the Anthropic adapter gates it per model/endpoint).
    agent = _after_fallback(_anthropic_primary(), model="claude-opus-5-5", provider="anthropic",
                            base_url=ANTHROPIC_URL, api_mode="anthropic_messages")
    assert fast_mode.effective_request_overrides(agent) == {"speed": "fast"}


def test_other_pinned_overrides_survive_the_route_change():
    agent = _after_fallback(
        _anthropic_primary(request_overrides={"speed": "fast", "extra_body": {"keep": 1}}),
        model="gpt-6-astra", provider="openai-api", base_url=OPENAI_URL, api_mode="codex_responses")
    assert fast_mode.effective_request_overrides(agent) == {"service_tier": "priority", "extra_body": {"keep": 1}}


def test_normal_tier_drops_a_stale_speed_pin_too():
    # ``/fast off`` after the pin, then a fallback: no window opens, the stale param still must not leak.
    agent = _after_fallback(_anthropic_primary(service_tier=None), model="gpt-6-astra", provider="openai-api",
                            base_url=OPENAI_URL, api_mode="codex_responses")
    assert fast_mode.effective_request_overrides(agent) == {}
