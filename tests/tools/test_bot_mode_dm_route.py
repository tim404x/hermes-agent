"""``bot_mode.dm_route``: where a local teammate's ``message_agent`` DM lands.

``side_window`` (the default) opens every bot-to-bot DM in its own tab, a titled side-window
session under the target bot, whether the target is busy or idle, so teammates' conversations
never mix into the user's own Bot Chat with that bot. ``auto`` is the earlier behaviour (a side
window only while the target's Bot Chat is busy) and ``main`` always uses the main Bot Chat.
The receiver's own config wins over the machine-root config.

One tab per conversation: a DM reuses the side window the same sender opened for the same
topic (or, without a topic, from the same sender session) while it was active in the last
6 hours and is not mid-turn; a busy one opens ``"<title> (2)"``.
"""

import json
import logging
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe, bot_relay

from tests.tools.test_bot_mode_dm import _FakeAgent, _capture_spawn, _managed_home


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


@pytest.fixture
def spawned(monkeypatch):
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    # The main Bot Chat is admitted through the profile authority (``_admit_live_dm``, upstream's
    # door), then a notification runner is spawned for the reply; a side window never admits there.
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda home, dm_file, author: {"status": "queued", "delivery_id": "d" * 64})
    return _capture_spawn(monkeypatch)


def _install(tmp_path, *, dm_route=None):
    home = _managed_home(tmp_path, teammates=("researcher",), dm_route=dm_route)
    return home, home / "profiles" / "researcher"


def _write_config(path: Path, route: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(f"bot_mode:\n  dm_route: {route}\n", encoding="utf-8")


def _runner(command):
    """``(flags, key, transport argv)`` of a runner command: the runner flags between the DM file
    and the transport argv, and the side-window key when one rides along."""
    parts = shlex.split(command)
    at = parts.index("--run-delivery") + 1
    if parts[at] == "--author":
        at += 2
    rest, flags, key = parts[at + 2:], [], None
    while rest and rest[0] in ("--profile-home", "--side-window", "--side-window-key"):
        flags.append(rest[0])
        if rest[0] == "--side-window-key":
            key = rest[1]
        rest = rest[1:] if rest[0] == "--side-window" else rest[2:]
    return flags, key, rest


def _bot_chat(profile_home: Path, *rows) -> None:
    """The target's canonical Bot Chat holding ``rows`` (role, age seconds)."""
    profile_home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.set_session_title("chat", "Bot Chat")
        for role, age in rows:
            db.append_message("chat", role, content=f"{role} row", timestamp=time.time() - age)
    finally:
        db.close()


def _send(home, message="check the micro1 call", *, topic="", session_id="sess-1"):
    agent = _FakeAgent(home)
    agent.session_id = session_id
    return json.loads(bot_mode_dm.message_agent_tool("researcher", message, agent=agent, topic=topic))


def _deliver(home, call, monkeypatch):
    """Run the spawned runner in-process against a fake transport, as the background process
    would: it stamps (and creates) the side window, then runs the turn."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(subprocess, "run",
                        lambda argv, **_k: subprocess.CompletedProcess(argv, 0, stdout="REPLY", stderr=""))
    assert bot_mode_dm._delivery_main(shlex.split(call["command"])[2:]) == 0


def _session_for(profile_home: Path, title: str) -> str | None:
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        return db.resolve_session_by_title(title)
    finally:
        db.close()


# ── routing ──────────────────────────────────────────────────────────────────


def test_default_route_opens_a_side_window_even_when_the_target_is_idle(tmp_path, spawned, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda *a, **k: pytest.fail("a side window never queues into the Bot Chat"))
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))
    assert side.bot_chat_busy(target) == (False, "")

    result = _send(home, topic="micro1 | Pricing + Volume")

    assert result["status"] == "queued"
    assert result["routed"] == "side_window"
    assert result["window"] == "micro1 | Pricing + Volume"
    assert "Do NOT resend" in result["detail"]
    [call] = spawned
    flags, key, argv = _runner(call["command"])
    assert flags == ["--profile-home", "--side-window", "--side-window-key"]
    assert key
    assert argv == ["hermes", "-p", "researcher", "chat", "--in", "~", "-c", "micro1 | Pricing + Volume",
                    "--create-if-missing", "-Q"]


def test_auto_route_keeps_todays_behaviour(tmp_path, spawned):
    home, target = _install(tmp_path, dm_route="auto")
    _bot_chat(target, ("user", 30), ("assistant", 20))

    idle = _send(home, topic="micro1")
    assert "routed" not in idle
    flags, _, argv = _runner(spawned[-1]["command"])
    assert flags == ["--profile-home"]
    assert argv == ["hermes", "-p", "researcher"]

    _bot_chat(target, ("user", 5))
    busy = _send(home, topic="micro1")
    assert busy["routed"] == "side_window"
    assert "was busy" in busy["detail"]
    flags, key, argv = _runner(spawned[-1]["command"])
    assert flags == ["--profile-home", "--side-window"]
    assert key is None
    assert argv[argv.index("-c") + 1] == "micro1"


def test_main_route_always_uses_the_main_bot_chat(tmp_path, spawned):
    home, target = _install(tmp_path, dm_route="main")
    _bot_chat(target, ("user", 5))
    assert side.bot_chat_busy(target)[0] is True

    result = _send(home, topic="micro1")

    assert "routed" not in result
    flags, _, argv = _runner(spawned[0]["command"])
    assert "--side-window" not in flags
    assert argv == ["hermes", "-p", "researcher"]


def test_the_receivers_config_overrides_the_global_one(tmp_path, spawned):
    home, target = _install(tmp_path, dm_route="main")
    _write_config(target, "side_window")
    assert _send(home, topic="micro1")["routed"] == "side_window"

    _write_config(home, "side_window")
    _write_config(target, "main")
    assert "routed" not in _send(home, topic="micro1")


def test_an_unknown_receiver_value_falls_back_to_the_global_one(tmp_path, spawned):
    home, target = _install(tmp_path, dm_route="main")
    _write_config(target, "sideways")
    assert "routed" not in _send(home, topic="micro1")


def test_a_failing_side_window_path_falls_back_to_auto(tmp_path, spawned, monkeypatch, caplog):
    def boom(*_a, **_k):
        raise RuntimeError("state.db exploded")

    monkeypatch.setattr(side, "resolve_window", boom)
    home, target = _install(tmp_path)
    _bot_chat(target, ("user", 30), ("assistant", 20))

    with caplog.at_level(logging.WARNING, logger="tools.bot_mode_dm"):
        idle = _send(home, topic="micro1")
    assert "routed" not in idle
    flags, _, argv = _runner(spawned[-1]["command"])
    assert argv == ["hermes", "-p", "researcher"]
    assert any("falling back" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    _bot_chat(target, ("user", 5))
    busy = _send(home, topic="micro1")
    assert busy["routed"] == "side_window"
    flags, _, argv = _runner(spawned[-1]["command"])
    assert flags == ["--profile-home", "--side-window"]
    assert argv[argv.index("-c") + 1] == "micro1"


# ── one tab per conversation ─────────────────────────────────────────────────


def test_same_sender_and_topic_within_six_hours_reuses_the_tab(tmp_path, spawned, monkeypatch):
    home, target = _install(tmp_path)
    first = _send(home, topic="micro1 | Pricing")
    _deliver(home, spawned[0], monkeypatch)
    window_id = _session_for(target, "micro1 | Pricing")
    assert window_id

    again = _send(home, "and the volume ask?", topic="  Micro1 |  pricing ")

    assert again["window"] == first["window"] == "micro1 | Pricing"
    assert _runner(spawned[1]["command"])[1] == _runner(spawned[0]["command"])[1]
    _deliver(home, spawned[1], monkeypatch)
    assert _session_for(target, "micro1 | Pricing") == window_id


def test_without_a_topic_one_sender_session_keeps_one_tab(tmp_path, spawned, monkeypatch):
    home, _ = _install(tmp_path)
    first = _send(home, "please review the Q3 volume numbers today")
    _deliver(home, spawned[0], monkeypatch)

    same_session = _send(home, "also the Q4 forecast")
    other_session = _send(home, "also the Q4 forecast", session_id="sess-2")

    assert first["window"] == "hermes: please review the Q3 volume numbers"
    assert same_session["window"] == first["window"]
    assert other_session["window"] == "hermes: also the Q4 forecast"


def test_a_different_topic_opens_a_new_tab(tmp_path, spawned, monkeypatch):
    home, _ = _install(tmp_path)
    _send(home, topic="micro1")
    _deliver(home, spawned[0], monkeypatch)

    other = _send(home, topic="payroll")

    assert other["window"] == "payroll"
    assert _runner(spawned[1]["command"])[1] != _runner(spawned[0]["command"])[1]


def test_a_new_sender_on_an_existing_title_opens_a_new_tab(tmp_path, spawned, monkeypatch):
    """The key names the sender: another bot's tab with the same title is never reused."""
    home, target = _install(tmp_path)
    side.stamp_side_window(target, "micro1", author={"id": "bot:atlas", "name": "atlas", "is_bot": True},
                           key="bot:atlas>researcher|topic:micro1")

    result = _send(home, topic="micro1")

    assert result["window"] == "micro1 (2)"


def _age_window(profile_home: Path, session_id: str, seconds: float) -> None:
    """Push a window's whole activity record ``seconds`` into the past."""
    then = time.time() - seconds
    conn = sqlite3.connect(profile_home / "state.db")
    try:
        origin = json.loads(conn.execute("SELECT origin_json FROM sessions WHERE id = ?",
                                          (session_id,)).fetchone()[0])
        origin["side_window"]["at"] = then
        conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = ?, origin_json = ? WHERE id = ?",
                     (then, then, json.dumps(origin), session_id))
        conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (then, session_id))
        conn.commit()
    finally:
        conn.close()


def test_after_six_hours_a_new_tab_opens(tmp_path, spawned, monkeypatch):
    home, target = _install(tmp_path)
    _send(home, topic="micro1")
    _deliver(home, spawned[0], monkeypatch)
    window_id = _session_for(target, "micro1")

    _age_window(target, window_id, 5 * 3600)
    assert _send(home, topic="micro1")["window"] == "micro1"

    _age_window(target, window_id, 7 * 3600)
    assert _send(home, topic="micro1")["window"] == "micro1 (2)"


def test_a_busy_reused_tab_opens_a_numbered_tab(tmp_path, spawned, monkeypatch):
    home, target = _install(tmp_path)
    _send(home, topic="micro1")
    _deliver(home, spawned[0], monkeypatch)
    window_id = _session_for(target, "micro1")
    db = SessionDB(db_path=target / "state.db")
    try:
        db.append_message(window_id, "user", content="mid-turn", timestamp=time.time())
    finally:
        db.close()

    result = _send(home, topic="micro1")

    assert result["window"] == "micro1 (2)"
    # The numbered tab joins the conversation: once it exists and the first is still busy, it is reused.
    _deliver(home, spawned[1], monkeypatch)
    assert _send(home, topic="micro1")["window"] == "micro1 (2)"


def test_a_tab_whose_turn_lock_is_held_counts_as_busy(tmp_path, spawned, monkeypatch):
    home, _ = _install(tmp_path)
    _send(home, topic="micro1")
    _deliver(home, spawned[0], monkeypatch)

    with bot_relay.acquire_turn_lock(home, side.side_window_lock_name("researcher", "micro1"), timeout_seconds=0):
        assert _send(home, topic="micro1")["window"] == "micro1 (2)"
    assert _send(home, topic="micro1")["window"] == "micro1"
