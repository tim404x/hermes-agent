"""Static ``priority`` fast mode reaches the wire for every surface, incl. direct OpenAI keys.

Two bugs, one contract:

1. ``openai-api`` (the registry slug for a direct ``api.openai.com`` key) was missing from the
   fast-mode route allowlist, so ``resolve_fast_mode_overrides`` returned ``None`` and ``/fast``
   was a silent no-op for every direct-key user (only the catalog-only ``openai`` slug passed).
2. ``effective_request_overrides`` only layered the fast override for the bounded ``auto`` /
   ``cold`` windows. Surfaces that construct the agent with ``service_tier="priority"`` but
   never pin ``request_overrides`` (desktop ``_make_agent``, subagents) sent standard-tier
   requests while the UI showed fast on.

Also: ``gpt-6`` accepts ``reasoning.effort: max`` (live-verified), so the Codex effort clamp
must not silently downgrade it to ``xhigh``.
"""

from types import SimpleNamespace

from agent import fast_mode
from agent.reasoning_effort import clamp_effort, codex_supported_efforts
from hermes_cli.models import resolve_fast_mode_overrides

OPENAI_URL = "https://api.openai.com/v1"


def _agent(**kw):
    base = dict(
        service_tier="priority",
        model="gpt-6-astra",
        provider="openai-api",
        base_url=OPENAI_URL,
        api_mode="codex_responses",
        request_overrides={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_direct_openai_key_slug_is_a_fast_mode_route():
    # Both spellings of "first-party OpenAI" resolve to the same wire param.
    assert resolve_fast_mode_overrides("gpt-6-astra", provider="openai-api", base_url=OPENAI_URL) == {
        "service_tier": "priority"
    }
    assert resolve_fast_mode_overrides("gpt-6-astra", provider="openai", base_url=OPENAI_URL) == {
        "service_tier": "priority"
    }
    # A proxy in front of the same model still gets nothing (billing lives at the first party).
    assert resolve_fast_mode_overrides("gpt-6-astra", provider="openai-api", base_url="https://proxy.test/v1") is None


def test_static_priority_reaches_wire_without_prepinned_overrides():
    # Desktop / subagent shape: service_tier set, request_overrides untouched.
    agent = _agent()
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "priority"
    assert agent.request_overrides == {}  # never mutated

    # Explicit normal stays normal.
    assert "service_tier" not in fast_mode.effective_request_overrides(_agent(service_tier=None))

    # CLI / gateway shape (already pinned) is idempotent — no duplicate or conflicting value.
    pinned = _agent(request_overrides={"service_tier": "priority", "extra_body": {"keep": 1}})
    assert fast_mode.effective_request_overrides(pinned) == {"service_tier": "priority", "extra_body": {"keep": 1}}


def test_static_priority_is_route_gated_like_the_bounded_windows():
    # Same gate as auto/cold: an unsupported route (proxy) must not receive the param.
    proxied = _agent(base_url="https://proxy.test/v1")
    assert "service_tier" not in fast_mode.effective_request_overrides(proxied)
    # Anthropic route picks the speed param instead.
    anth = _agent(model="claude-opus-5", provider="anthropic", base_url="https://api.anthropic.com",
                  api_mode="anthropic_messages")
    assert fast_mode.effective_request_overrides(anth) == {"speed": "fast"}


def test_gpt6_effort_ladder_keeps_max():
    supported = codex_supported_efforts("gpt-6-astra")
    assert clamp_effort("max", supported) == "max"
    assert clamp_effort("high", supported) == "high"
    # Older generations still clamp max -> xhigh (the documented ceiling).
    assert clamp_effort("max", codex_supported_efforts("gpt-5.4")) == "xhigh"
