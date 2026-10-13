"""``message_agent`` side window: a teammate whose Bot Chat is busy gets the DM in a new
titled session instead of having it queued into (or racing) its main Bot Chat.

Busy = the Bot Chat tip is mid-turn (fresh, unfinished newest row), its live mailbox
already holds a pending delivery, or a CLI delivery turn holds its turn lock.
Idle teammates, peers and relay targets keep today's path untouched.
"""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe, bot_relay

from tests.tools.test_bot_mode_dm import _FakeAgent, _capture_spawn, _managed_home


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _bot_chat(profile_home: Path, *rows) -> None:
    """A real state.db whose canonical Bot Chat holds ``rows`` (role, tool_calls, age seconds)."""
    from hermes_state import SessionDB

    profile_home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=profile_home / "state.db")
    try:
        db.create_session(session_id="chat", source="desktop")
        db.set_session_title("chat", "Bot Chat")
        for role, tool_calls, age in rows:
            db.append_message("chat", role, content=f"{role} row", tool_calls=tool_calls,
                              tool_call_id="call_1" if role == "tool" else None,
                              timestamp=time.time() - age)
    finally:
        db.close()


_CALL = [{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]


def _target(tmp_path):
    home = _managed_home(tmp_path, teammates=("researcher",))
    return home, home / "profiles" / "researcher"


def _runner(command):
    """(runner flags between dm_file and the transport argv, transport argv) of a runner command."""
    parts = shlex.split(command)
    marker = parts.index("--run-delivery") + 1
    if parts[marker] == "--author":
        marker += 2
    rest = parts[marker + 2:]
    flags = []
    while rest and rest[0] in ("--profile-home", "--side-window"):
        take = 2 if rest[0] == "--profile-home" else 1
        flags.append(rest[0])
        rest = rest[take:]
    return flags, rest


# ── busy detection ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("rows", [
    [("user", None, 5)],
    [("user", None, 30), ("assistant", _CALL, 20)],
    [("user", None, 30), ("assistant", _CALL, 20), ("tool", None, 10)],
], ids=["user", "assistant-tool-calls", "tool"])
def test_unfinished_fresh_tip_is_busy(tmp_path, rows):
    _, target = _target(tmp_path)
    _bot_chat(target, *rows)
    busy, why = side.bot_chat_busy(target)
    assert busy is True
    assert "mid-turn" in why


def test_finished_reply_is_idle(tmp_path):
    _, target = _target(tmp_path)
    _bot_chat(target, ("user", None, 30), ("assistant", None, 20))
    assert side.bot_chat_busy(target) == (False, "")


def test_unfinished_tip_older_than_fifteen_minutes_is_idle(tmp_path):
    """A crashed turn leaves an unanswered row forever; it must not read as busy forever."""
    _, target = _target(tmp_path)
    _bot_chat(target, ("user", None, 16 * 60))
    assert side.bot_chat_busy(target) == (False, "")
    _bot_chat(target, ("user", None, 14 * 60))
    assert side.bot_chat_busy(target)[0] is True


def test_no_bot_chat_is_idle(tmp_path):
    _, target = _target(tmp_path)
    assert side.bot_chat_busy(target) == (False, "")


def _authority_receipt(target, key, status):
    """The receipt the profile authority's mailbox writes for one admitted Bot Chat delivery
    (``gateway/session_bot_mailbox.write_receipt``): the busy probe reads these files."""
    from tools import bot_live_delivery as live

    root = live._root(target)
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{key}.json").write_text(json.dumps(dict(
        delivery_id=key, profile_home=str(target.resolve()), session_id="chat", principal_id="uid:1",
        admission_id="adm-" + key[:4], message="first", status=status)), encoding="utf-8")


@pytest.mark.parametrize("status", ["canonical", "queued", "claimed"])
def test_pending_live_mailbox_delivery_is_busy(tmp_path, status):
    _, target = _target(tmp_path)
    _authority_receipt(target, "a" * 32, status)
    busy, why = side.bot_chat_busy(target)
    assert busy is True
    assert "waiting" in why


@pytest.mark.parametrize("status", ["settled", "failed", "cancelled", "ambiguous"])
def test_settled_or_abandoned_mailbox_delivery_is_idle(tmp_path, status):
    _, target = _target(tmp_path)
    _authority_receipt(target, "b" * 32, status)
    assert side.bot_chat_busy(target) == (False, "")


def test_a_pending_receipt_older_than_the_runner_wait_is_idle(tmp_path, monkeypatch):
    # A receipt older than the live runner's wait has nobody waiting on it (claims never expire).
    _, target = _target(tmp_path)
    _authority_receipt(target, "c" * 32, "queued")
    monkeypatch.setattr(bot_mode_dm, "_LIVE_WAIT_MAX_SECONDS", 0)
    assert side.bot_chat_busy(target) == (False, "")


def test_held_cli_turn_lock_is_busy(tmp_path):
    home, target = _target(tmp_path)
    assert side.bot_chat_busy(target) == (False, "")
    with bot_relay.acquire_turn_lock(home, "researcher", timeout_seconds=0):
        busy, why = side.bot_chat_busy(target)
        assert busy is True
        assert "delivery turn" in why
    assert side.bot_chat_busy(target) == (False, "")


def test_read_errors_read_as_idle(tmp_path, monkeypatch):
    _, target = _target(tmp_path)
    target.mkdir(parents=True, exist_ok=True)
    (target / "state.db").write_bytes(b"this is not a sqlite database" * 64)
    assert side.bot_chat_busy(target) == (False, "")

    import hermes_state

    def boom(*_a, **_k):
        raise OSError("disk gone")

    _bot_chat(target.parent / "other")
    monkeypatch.setattr(hermes_state, "SessionDB", boom)
    assert side.bot_chat_busy(target.parent / "other") == (False, "")


# ── window title ─────────────────────────────────────────────────────────────


def test_title_comes_from_topic_sanitized():
    assert side.side_window_title("micro1 | Pricing + Volume", "Atlas", "hey") == "micro1 | Pricing + Volume"
    assert side.side_window_title(' "micro1"\n  pricing\t\'sheet\' ', "Atlas", "hey") == "micro1 pricing sheet"
    assert side.side_window_title("--resume x", "Atlas", "hey") == "resume x"
    assert len(side.side_window_title("x" * 200, "Atlas", "hey")) == 60


def test_title_falls_back_to_sender_and_first_words():
    message = 'check the micro1 call\n"wispr" + the price sheet and the volume ask'
    assert side.side_window_title("", "Atlas", message) == "Atlas: check the micro1 call wispr +"
    assert side.side_window_title(" \n ", "Atlas", "short") == "Atlas: short"


@pytest.mark.parametrize("topic", ["Bot Chat", "bot chat", ' "Bot  Chat" '])
def test_title_is_never_the_bot_chat(topic):
    assert side.side_window_title(topic, "Atlas", "hey").casefold() != "bot chat"


# ── routing through message_agent ────────────────────────────────────────────


def test_busy_teammate_gets_a_side_window(tmp_path, monkeypatch):
    calls = _capture_spawn(monkeypatch)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda *a, **k: pytest.fail("a side window must never queue into the Bot Chat"))
    home, target = _target(tmp_path)
    _bot_chat(target, ("user", None, 5))

    result = json.loads(bot_mode_dm.message_agent_tool(
        "researcher", "check the micro1 call", agent=_FakeAgent(home), topic="micro1 | Pricing + Volume"))

    assert result["status"] == "queued"
    assert result["routed"] == "side_window"
    assert result["window"] == "micro1 | Pricing + Volume"
    assert result["to"] == "@researcher"
    assert "busy (mid-turn" in result["detail"]
    assert "side window 'micro1 | Pricing + Volume'" in result["detail"]
    assert "Do NOT resend" in result["detail"]
    [call] = calls
    flags, argv = _runner(call["command"])
    assert flags == ["--profile-home", "--side-window"]
    assert argv == ["hermes", "-p", "researcher", "chat", "--in", "~", "-c", "micro1 | Pricing + Volume",
                    "--create-if-missing", "-Q"]


def test_busy_teammate_without_topic_gets_the_fallback_title(tmp_path, monkeypatch):
    calls = _capture_spawn(monkeypatch)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    home, target = _target(tmp_path)
    _bot_chat(target, ("user", None, 5))

    result = json.loads(bot_mode_dm.message_agent_tool(
        "researcher", "please review the Q3 volume numbers today", agent=_FakeAgent(home)))

    assert result["window"] == "hermes: please review the Q3 volume numbers"
    _, argv = _runner(calls[0]["command"])
    assert argv[argv.index("-c") + 1] == "hermes: please review the Q3 volume numbers"


def test_idle_teammate_keeps_the_exact_bot_chat_path(tmp_path, monkeypatch):
    calls = _capture_spawn(monkeypatch)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    admitted = []
    # The Bot Chat is admitted through the profile authority; the spawned runner only waits for the reply.
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm", lambda *a, **k: admitted.append(a)
                        or {"status": "queued", "delivery_id": "c" * 64})
    home, target = _target(tmp_path)
    _bot_chat(target, ("user", None, 30), ("assistant", None, 20))

    result = json.loads(bot_mode_dm.message_agent_tool(
        "researcher", "hello", agent=_FakeAgent(home), topic="ignored when idle"))

    assert "routed" not in result and "window" not in result
    assert len(admitted) == 1
    flags, argv = _runner(calls[0]["command"])
    assert flags == ["--profile-home"]
    assert argv == ["hermes", "-p", "researcher"]


def test_peer_and_relay_targets_never_take_a_side_window(tmp_path, monkeypatch):
    calls = _capture_spawn(monkeypatch)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(side, "bot_chat_busy", lambda *a, **k: pytest.fail("only local teammates are probed"))
    home = _managed_home(tmp_path, teammates=("researcher",), peers=("spark",))
    bot_relay.write_remote_roster(home, [
        {"profile": "default", "handle": "hermes", "connection_id": "cloud-1", "connection_label": "Hermes Cloud"},
    ])

    peer = json.loads(bot_mode_dm.message_agent_tool("spark/researcher", "ping", agent=_FakeAgent(home), topic="t"))
    relay = json.loads(bot_mode_dm.message_agent_tool("hermes", "ping", agent=_FakeAgent(home), topic="t"))

    assert "routed" not in peer and "routed" not in relay
    assert all("--side-window" not in call["command"] for call in calls)


def test_schema_offers_an_optional_topic():
    schema = bot_mode_dm.message_agent_tool_schema()["function"]
    assert "topic" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["target", "message"]
    assert "side window" in schema["description"]


def test_inline_executor_forwards_the_topic(tmp_path, monkeypatch):
    from agent import inline_tool_executors

    seen = {}
    monkeypatch.setattr(bot_mode_dm, "message_agent_tool", lambda **kw: seen.update(kw) or "{}")
    ctx = type("Ctx", (), {"effective_task_id": "t1"})()
    inline_tool_executors._RAW_INLINE_TOOL_EXECUTORS["message_agent"](
        object(), {"target": "researcher", "message": "hi", "topic": "micro1"}, ctx)
    assert seen["topic"] == "micro1"


# ── runner ───────────────────────────────────────────────────────────────────


def _fake_run(monkeypatch, *responses):
    calls = []
    queue = list(responses)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _side_argv(title="micro1"):
    return ["hermes", "-p", "researcher", "chat", "--in", "~", "-c", title, "--create-if-missing", "-Q"]


def test_runner_side_window_flag_skips_live_admission(tmp_path, monkeypatch, capsys):
    home, target = _target(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda *a, **k: pytest.fail("side windows never use live admission"))
    calls = _fake_run(monkeypatch, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")
    command = bot_mode_dm._delivery_command(_side_argv(), str(dm_file), stdin_file=False,
                                            profile_home=target, side_window=True)

    assert bot_mode_dm._delivery_main(shlex.split(command)[2:]) == 0

    assert calls == [[*_side_argv(), "--query-file", str(dm_file)]]
    assert capsys.readouterr().out == "REPLY"
    assert not dm_file.exists()


def test_side_window_lock_is_keyed_per_title(tmp_path, monkeypatch):
    home, _ = _target(tmp_path)
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(home))
    monkeypatch.setattr(bot_relay, "turn_wait_seconds", lambda: 0.0)
    lock = bot_mode_dm._delivery_lock

    # The busy Bot Chat's CLI turn never blocks a side window, and different titles run in parallel.
    with lock(["hermes", "-p", "researcher"], stdin_file=False):
        with lock(_side_argv("alpha"), stdin_file=False, side_window=True):
            with lock(_side_argv("beta"), stdin_file=False, side_window=True):
                # The same title serializes.
                with pytest.raises(bot_relay.TurnBusyError):
                    with lock(_side_argv("alpha"), stdin_file=False, side_window=True):
                        pass


def test_session_not_owned_side_window_retries_once_in_a_numbered_window(tmp_path, monkeypatch, capsys):
    home, target = _target(tmp_path)
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(home))
    not_owned = subprocess.CompletedProcess([], 1, stdout="", stderr="hermes-refusal-reason: SESSION_NOT_OWNED\n")
    calls = _fake_run(monkeypatch, not_owned, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), str(dm_file), stdin_file=False,
                                     profile_home=target, side_window=True)

    assert code == 0
    assert [c[c.index("-c") + 1] for c in calls] == ["micro1", "micro1 (2)"]
    assert capsys.readouterr().out == "REPLY"


def test_session_not_owned_side_window_refuses_after_its_one_retry(tmp_path, monkeypatch, capsys):
    home, target = _target(tmp_path)
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(home))
    not_owned = subprocess.CompletedProcess([], 1, stdout="", stderr="hermes-refusal-reason: SESSION_NOT_OWNED\n")
    calls = _fake_run(monkeypatch, not_owned, not_owned)
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), str(dm_file), stdin_file=False,
                                     profile_home=target, side_window=True)

    assert code == 1
    assert len(calls) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "target_busy"
    assert "micro1 (2)" in payload["error"]


# The Bot Chat CLI-lane refusal test (``_run_local_turn`` on a Bot Chat argv) retired with upstream's
# authority cutover: a Bot Chat DM is admitted only through the profile authority (``_admit_live_dm``,
# test_idle_teammate_keeps_the_exact_bot_chat_path); ``_run_local_turn`` runs side windows only.


def test_real_side_window_runner_round_trip(tmp_path):
    """Through a real subprocess, the runner command carries the flag to the transport argv
    unchanged and never pins a live-delivery intent for a side window."""
    home, target = _target(tmp_path)
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("secret", encoding="utf-8")
    observed = tmp_path / "observed.json"
    child = tmp_path / "child.py"
    child.write_text("import json, pathlib, sys\n"
                     "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
                     "print('REPLY')\n", encoding="utf-8")
    argv = [sys.executable, str(child), str(observed), "-c", "micro1"]
    command = bot_mode_dm._delivery_command(argv, str(dm_file), stdin_file=False, profile_home=target,
                                            side_window=True)

    result = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True,
                            env={**os.environ, "HERMES_HOME": str(home)})

    assert result.returncode == 0, result.stderr
    assert result.stdout == "REPLY\n"
    assert json.loads(observed.read_text(encoding="utf-8")) == ["-c", "micro1", "--query-file", str(dm_file)]
    assert not dm_file.exists()
    assert not Path(bot_mode_dm._live_intent_file(dm_file)).exists()


def test_fallback_title_uses_short_bot_name_and_skips_self_intro():
    from tools import bot_mode_dm_side_window as side
    assert side.side_window_title("", "Neo | Chief of Staff", "Neo here: template batches and --gap are LIVE now") \
        == "Neo: template batches and --gap are LIVE"
    assert side.side_window_title("", "Atlas", "check the micro1 call") == "Atlas: check the micro1 call"
