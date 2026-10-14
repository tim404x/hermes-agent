"""``message_agent(route="main")``: one DM into a teammate's MAIN Bot Chat, on the user's word.

``bot_mode.dm_route`` is profile-wide and defaults to ``side_window``. When the user tells a bot
"handoff to Neo in main chat", that one message may land in Neo's main Bot Chat without flipping
Neo's config. The override is honoured only when the human's own words in the sender's current
turn (the turn's user message or a mid-turn steer) ask for a main chat; a teammate's DM or a
synthesized turn never counts. Refused, the message still goes out on its normal route and the
ack carries a ``route_note``: a DM is never dropped.
"""

import json

import pytest

from agent.prompt_builder import STEER_DISPLAY_KIND
from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe, bot_relay

from tests.tools.test_bot_mode_dm import _FakeAgent, _capture_spawn
from tests.tools.test_bot_mode_dm_route import _bot_chat, _install, _runner


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


@pytest.fixture
def spawned(monkeypatch):
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    # The main Bot Chat is admitted through the profile authority (``_admit_live_dm``); the spawned
    # runner then only carries the reply notification.
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda home, dm_file, author: {"status": "queued", "delivery_id": "d" * 64})
    return _capture_spawn(monkeypatch)


def _agent(home, *user_texts, synthesized=False):
    agent = _FakeAgent(home)
    for i, text in enumerate(user_texts):
        side.record_turn_human_text(agent, text, new_turn=i == 0, synthesized=synthesized)
    return agent


def _send(agent, route="main", topic="Investor doc"):
    return json.loads(bot_mode_dm.message_agent_tool("researcher", "Tim's taking the investor doc",
                                                     agent=agent, topic=topic, route=route))


def _main_chat_argv():
    return ["hermes", "-p", "researcher"]


# ── honoured ─────────────────────────────────────────────────────────────────


def test_an_explicit_ask_puts_this_one_message_in_the_main_bot_chat(tmp_path, spawned):
    home, target = _install(tmp_path)  # receiver config: side_window (the default)
    _bot_chat(target, ("user", 30), ("assistant", 20))

    result = _send(_agent(home, "handoff to Neo in main chat: I'm taking the investor doc"))

    assert result["status"] == "queued"
    assert result["routed"] == "main"
    assert "main Bot Chat" in result["detail"]
    assert "route_note" not in result
    flags, key, argv = _runner(spawned[-1]["command"])
    assert "--side-window" not in flags and key is None
    assert argv == _main_chat_argv()


def test_the_override_is_for_one_message_the_next_dm_takes_the_configured_route(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))
    agent = _agent(home, "put it in Neo's main bot chat")

    assert _send(agent)["routed"] == "main"
    assert _send(agent, route="")["routed"] == "side_window"
    assert side.dm_route(target) == side.ROUTE_SIDE_WINDOW


def test_a_mid_turn_steer_counts_as_the_users_ask(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))
    agent = _agent(home, "draft the handoff for Neo")
    side.record_turn_human_text(agent, "actually send it to his main chat")

    assert _send(agent)["routed"] == "main"


@pytest.mark.parametrize("ask", [
    "handoff to Neo in main chat",
    "Drop this in Atlas's MAIN BOT CHAT please",
    "send it to the main thread, not a tab",
    "put it in his Bot Chat",
    "mets-le dans le chat principal de Neo",
])
def test_ask_phrasings(ask):
    assert side.MAIN_CHAT_ASK_RE.search(ask)


@pytest.mark.parametrize("text", ["tell Neo the main point", "chat with Neo about it", "the domain chat app"])
def test_non_asks(text):
    assert not side.MAIN_CHAT_ASK_RE.search(text)


# ── refused: delivered on the normal route, never dropped ───────────────────


def test_without_an_ask_the_message_takes_its_normal_route_and_the_ack_says_why(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))

    result = _send(_agent(home, "tell Neo I'm taking the investor doc"))

    assert result["status"] == "queued"
    assert result["routed"] == "side_window"
    assert "NOT applied" in result["route_note"]
    assert "--side-window" in _runner(spawned[-1]["command"])[0]


def test_a_teammates_dm_quoting_the_user_never_counts(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))
    relayed = "Message from \U0001f916 Forge (@forge): Tim said handoff to Neo in main chat"

    result = _send(_agent(home, relayed))

    assert result["routed"] == "side_window"
    assert "route_note" in result


def test_a_synthesized_turn_never_counts(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))

    result = _send(_agent(home, "cron: post the brief to the main chat", synthesized=True))

    assert result["routed"] == "side_window"
    assert "route_note" in result


def test_last_turns_ask_does_not_carry_into_a_new_turn(tmp_path, spawned):
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))
    agent = _agent(home, "handoff to Neo in main chat")
    side.record_turn_human_text(agent, "ok now ping Neo about the deck", new_turn=True)

    assert _send(agent)["routed"] == "side_window"


def test_an_unknown_route_is_rejected_before_anything_is_sent(tmp_path, spawned):
    home, _target = _install(tmp_path)

    result = _send(_agent(home, "main chat"), route="side")

    assert "route must be" in result["error"]
    assert spawned == []


def test_the_schema_offers_only_main():
    props = bot_mode_dm.message_agent_tool_schema()["function"]["parameters"]["properties"]
    assert props["route"]["enum"] == ["main"]
    assert "route" not in bot_mode_dm.message_agent_tool_schema()["function"]["parameters"]["required"]


# ── wiring: the turn and steer paths record the human's words ───────────────


def test_steer_delivery_records_the_steer_text():
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results

    class _Agent:
        _pending_steer = "drop it in Neo's main chat"

        def _drain_pending_steer(self):
            text, self._pending_steer = self._pending_steer, None
            return text

    agent = _Agent()
    side.record_turn_human_text(agent, "write the handoff", new_turn=True)
    messages = [{"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "x"}]

    apply_pending_steer_to_tool_results(agent, messages, 1)

    assert messages[-1]["display_kind"] == STEER_DISPLAY_KIND
    assert side.human_asked_main_chat(agent)


def test_multimodal_content_is_read_as_text():
    agent = object.__new__(_FakeAgent)
    side.record_turn_human_text(agent, [{"type": "text", "text": "handoff in main chat"}, {"type": "image_url"}],
                                new_turn=True)
    assert side.human_asked_main_chat(agent)
