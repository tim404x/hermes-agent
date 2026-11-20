"""``message_agent(new_tab=true)``: a bot opens a NEW task tab of its own in one tool call.

Before this, a bot asked to "work this in a new tab" told the user to open a chat and paste a brief:
``message_agent`` to yourself was refused and the only way was a hand-run runner recipe (Tim, 20 Nov
2026: "i thought we fixed that yesterday omg"). Now ``new_tab=true`` with the bot's own name (or
``me``) runs the brief as the bot's own task tab: the same side-window runner a teammate's DM uses,
in the caller's own profile, authored ``bot:<self>`` so the brief paints as "Brief from <bot>". One
title is one tab. A plain self-message stays refused and names the new path; a tab opened from a
tab nests at most ``SELF_TAB_MAX_DEPTH`` deep (the turn author's runtime ``tab_depth``).
"""

import json
import shlex
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent.display_row_typing import self_brief_author
from agent.turn_author import parse_turn_author
from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe, bot_relay

from tests.tools.test_bot_mode_dm import _FakeAgent, _append, _capture_spawn, _managed_home, _new_session, _profile_store
from tests.tools.test_bot_mode_dm_route import _runner, _session_for
from tests.waiting import wait_until

BRIEF = "Audit hub.xyz SEO: crawl the top 20 pages, list broken meta tags, propose fixes."


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


@pytest.fixture
def spawned(monkeypatch):
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    # A task tab is a side window: it never queues into the caller's own Bot Chat.
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda *a, **k: pytest.fail("a task tab never queues into the Bot Chat"))
    monkeypatch.setattr(bot_mode_dm, "SELF_TAB_WAIT_SECONDS", 0)
    monkeypatch.setattr(side, "TAB_STAMP_POLL_SECONDS", 0.02)
    return _capture_spawn(monkeypatch)


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A managed install with the shipped default route; the caller is Kepler, a named profile."""
    home = _managed_home(tmp_path, teammates=("kepler", "apex"), dm_route=None)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home, home / "profiles" / "kepler"


def _kepler(me: Path, *, turn_author=None, session_id="bot-chat-1") -> _FakeAgent:
    agent = _FakeAgent(me)
    agent.session_id = session_id
    agent._turn_author = turn_author
    return agent


def _open(agent, message=BRIEF, **kwargs) -> dict:
    kwargs.setdefault("target", "kepler")
    kwargs.setdefault("new_tab", True)
    return json.loads(bot_mode_dm.message_agent_tool(message=message, agent=agent, **kwargs))


def _command_parts(command: str) -> dict:
    """The runner command's author, DM file and profile home."""
    parts = shlex.split(command)
    at = parts.index("--run-delivery") + 1
    author = json.loads(parts[at + 1]) if parts[at] == "--author" else None
    at += 2 if author is not None else 0
    return {"author": author, "dm_file": parts[at + 1], "profile_home": parts[parts.index("--profile-home") + 1]}


def _gateway_turn(profile_home: Path, title: str, brief: str, *, empty_first: bool = False, during=None) -> str:
    """What ``hermes -p <bot> chat -c <title> --create-if-missing`` does at the gateway: mint the row (the single
    writer), persist the brief as the turn's user row, then (after ``during``) the reply."""
    with _profile_store(profile_home) as store:
        session_id = store.sessions.resolve_by_title(title)
        if session_id is None:
            session_id = f"gw-{time.time_ns()}"
            _new_session(store, session_id, source="cli", title=title)
    if empty_first and during is not None:
        during(session_id, before_user_row=True)
    with _profile_store(profile_home) as store:
        _append(store, session_id, "user", brief)
    if during is not None:
        during(session_id, before_user_row=False)
    with _profile_store(profile_home) as store:
        _append(store, session_id, "assistant", "DONE")
    return session_id


def _run_runner(home: Path, call: dict, monkeypatch, *, empty_first: bool = False, during=None) -> list[str]:
    """Run the spawned runner in-process, as the background process would, against a fake gateway transport."""
    minted: list[str] = []

    def fake_transport(argv, **_k):
        assert argv[argv.index("-p") + 1] == "kepler"
        brief = Path(argv[argv.index("--query-file") + 1]).read_text(encoding="utf-8")
        title = argv[argv.index("-c") + 1]
        minted.append(_gateway_turn(home / "profiles" / "kepler", title, brief, empty_first=empty_first,
                                    during=during))
        return subprocess.CompletedProcess(argv, 0, stdout="DONE", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_transport)
    assert bot_mode_dm._delivery_main(shlex.split(call["command"])[2:]) == 0
    return minted


def _stamp(profile_home: Path, session_id: str) -> dict | None:
    from persistence.conversation.sessions import record_side_window_stamp

    with _profile_store(profile_home) as store:
        record = store.sessions.get(session_id)
        return record_side_window_stamp(record) if record else None


# ── one call opens the tab ───────────────────────────────────────────────────


def test_new_tab_runs_the_brief_in_a_tab_of_the_calling_bot(install, spawned):
    _home, me = install
    result = _open(_kepler(me), topic="SEO audit")

    assert result["ok"] is True and result["tab"] is True
    assert result["title"] == "SEO audit"
    assert result["routed"] == "new_tab" and result["reused"] is False
    assert result["status"] == "queued" and "Do NOT resend" in result["detail"]
    [call] = spawned
    flags, key, argv = _runner(call["command"])
    assert flags == ["--profile-home", "--side-window", "--side-window-key"]
    assert key == "bot:kepler>kepler|topic:seo audit"
    assert argv == ["hermes", "-p", "kepler", "chat", "--in", "~", "-c", "SEO audit", "--create-if-missing", "-Q"]
    parts = _command_parts(call["command"])
    assert Path(parts["profile_home"]) == me.resolve()
    assert parts["author"]["id"] == "bot:kepler" and parts["author"]["is_bot"] is True
    assert parts["author"]["tab_depth"] == 1
    # The brief rides as written, never behind a "Message from ..." signature, so the tab's first row is
    # typed bot_brief ("Brief from Kepler") at turn start.
    brief = Path(parts["dm_file"]).read_text(encoding="utf-8")
    assert brief == BRIEF
    assert self_brief_author(parse_turn_author(parts["author"]), "kepler", brief)["id"] == "bot:kepler"


@pytest.mark.parametrize("target", ["me", "", "@kepler", "self", "Kepler"])
def test_new_tab_targets_that_name_the_caller(install, spawned, target):
    _home, me = install
    result = _open(_kepler(me), topic="SEO audit", target=target)

    assert result["ok"] is True
    [call] = spawned
    assert _runner(call["command"])[2][:3] == ["hermes", "-p", "kepler"]


def test_new_tab_without_a_topic_is_titled_from_the_brief(install, spawned):
    _home, me = install
    message = "Kepler here: rebuild the deal heatmap with the November data. Then post it."
    result = _open(_kepler(me), message=message)

    title = side.side_window_title("", "Kepler", message)
    assert result["title"] == title == "Rebuild the deal heatmap with the November data"
    assert _runner(spawned[0]["command"])[1] == side.side_window_key("bot:kepler", "kepler", title, "")


def test_the_same_title_resumes_the_same_tab(install, spawned, monkeypatch):
    home, me = install
    first = _open(_kepler(me), topic="SEO audit")
    [window_id] = _run_runner(home, spawned[0], monkeypatch)
    assert _stamp(me, window_id)["by"] == "bot:kepler"

    again = _open(_kepler(me, session_id="another-chat"), message="Now the blog pages.", topic="  seo AUDIT ")

    assert again["title"] == first["title"] == "SEO audit"
    assert again["reused"] is True and again["session_id"] == window_id
    assert _runner(spawned[1]["command"])[1] == _runner(spawned[0]["command"])[1]
    _run_runner(home, spawned[1], monkeypatch)
    assert _session_for(me, "SEO audit") == window_id
    other = _open(_kepler(me), topic="Pricing page")
    assert other["title"] == "Pricing page" and other["reused"] is False


def test_a_second_call_while_the_first_turn_runs_lands_in_the_same_tab(install, spawned, monkeypatch):
    """The new tab is stamped as soon as the gateway mints it with the brief in it, so a follow-up on the same
    title during the first turn finds that tab instead of opening "SEO audit (2)"."""
    home, me = install
    _open(_kepler(me), topic="SEO audit")
    seen = {}

    def during(session_id, before_user_row):
        if before_user_row:
            return
        wait_until(lambda: _stamp(me, session_id), what="the tab stamped while its turn runs")
        seen.update(_open(_kepler(me), message="Add the blog pages too.", topic="SEO audit"), sid=session_id)

    _run_runner(home, spawned[0], monkeypatch, during=during)

    assert seen["title"] == "SEO audit" and seen["reused"] is True and seen["session_id"] == seen["sid"]


def test_a_new_tab_is_stamped_while_its_turn_runs_and_an_empty_row_is_never_dropped(install, spawned, monkeypatch):
    """The runner stamps a NEW window once the gateway's row holds the brief (the tab opens while the turn runs,
    not after it). A row the gateway minted a moment before writing the brief looks like a stale pre-mint, which
    the stamp drops: the early stamp waits for the brief instead."""
    home, me = install
    _open(_kepler(me), topic="SEO audit")
    checks, states = [], []
    real_check = side.window_has_brief
    monkeypatch.setattr(side, "window_has_brief", lambda *a, **k: checks.append(1) or real_check(*a, **k))

    def during(session_id, before_user_row):
        if before_user_row:
            seen = len(checks)
            wait_until(lambda: len(checks) >= seen + 3, what="three early-stamp checks of the empty row")
            with _profile_store(me) as store:
                states.append(("empty", store.sessions.get(session_id) is not None, _stamp(me, session_id)))
        else:
            states.append(("brief", True, wait_until(lambda: _stamp(me, session_id), what="the early stamp")))

    [window_id] = _run_runner(home, spawned[0], monkeypatch, empty_first=True, during=during)

    assert states[0] == ("empty", True, None)
    assert states[1][2]["by"] == "bot:kepler" and states[1][2]["key"] == "bot:kepler>kepler|topic:seo audit"
    assert _session_for(me, "SEO audit") == window_id


def test_the_ack_carries_the_new_tab_session_id_once_the_tab_is_stamped(install, monkeypatch):
    home, me = install
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: "hermes")
    monkeypatch.setattr(side, "TAB_STAMP_POLL_SECONDS", 0.02)
    monkeypatch.setattr(bot_mode_dm, "SELF_TAB_WAIT_SECONDS", 10)
    runners = []

    def fake_terminal_tool(command, **_kwargs):
        # The background runner starts on its own thread, as the tracked process would.
        runner = threading.Thread(target=_run_runner, args=(home, {"command": command}, monkeypatch))
        runners.append(runner)
        runner.start()
        return json.dumps({"output": "Background process started", "session_id": "proc_test1234"})

    import tools.terminal_tool as terminal_tool_module

    monkeypatch.setattr(terminal_tool_module, "terminal_tool", fake_terminal_tool)
    result = _open(_kepler(me), topic="SEO audit")
    for runner in runners:
        runner.join(10)

    assert result["session_id"] == _session_for(me, "SEO audit")
    assert "tab_pending" not in result


def test_a_tab_not_stamped_within_the_wait_is_reported_pending(install, spawned):
    _home, me = install
    result = _open(_kepler(me), topic="SEO audit")

    assert result["ok"] is True and result["session_id"] is None and result["tab_pending"] is True


# ── what stays refused ───────────────────────────────────────────────────────


@pytest.mark.parametrize("target", ["kepler", "me"])
def test_a_plain_self_message_is_refused_and_names_new_tab(install, spawned, target):
    _home, me = install
    result = json.loads(bot_mode_dm.message_agent_tool(target=target, message="hi", agent=_kepler(me)))

    assert "yourself" in result["error"] and "new_tab=true" in result["error"]
    assert spawned == []


def test_new_tab_never_opens_a_tab_under_a_teammate(install, spawned):
    _home, me = install
    result = _open(_kepler(me), topic="SEO audit", target="apex")

    assert "new_tab" in result["error"] and "@apex" in result["error"]
    assert spawned == []


def test_new_tab_rejects_route_main(install, spawned):
    _home, me = install
    result = _open(_kepler(me), topic="SEO audit", route="main")

    assert "route" in result["error"] and spawned == []


@pytest.mark.parametrize("depth, opened", [(None, 2), (1, 2), (2, 3)])
def test_a_tab_opened_from_a_tab_nests_one_deeper(install, spawned, depth, opened):
    _home, me = install
    author = {"id": "bot:kepler", "name": "kepler", "is_bot": True, **({"tab_depth": depth} if depth else {})}
    result = _open(_kepler(me, turn_author=parse_turn_author(author)), topic="Sub task")

    assert result["ok"] is True
    assert _command_parts(spawned[0]["command"])["author"]["tab_depth"] == opened


def test_no_tab_opens_past_the_depth_cap(install, spawned):
    _home, me = install
    author = parse_turn_author({"id": "bot:kepler", "name": "kepler", "is_bot": True,
                                "tab_depth": bot_mode_dm.SELF_TAB_MAX_DEPTH})
    result = _open(_kepler(me, turn_author=author), topic="Sub task")

    assert "error" in result and str(bot_mode_dm.SELF_TAB_MAX_DEPTH) in result["error"]
    assert spawned == []


def test_a_teammates_turn_starts_a_fresh_depth(install, spawned):
    """Depth follows the bot's OWN tab chain: a turn a teammate started is not one of Kepler's tabs."""
    _home, me = install
    author = parse_turn_author({"id": "bot:apex", "name": "apex", "is_bot": True, "tab_depth": 3})
    result = _open(_kepler(me, turn_author=author), topic="Delivery build")

    assert result["ok"] is True
    assert _command_parts(spawned[0]["command"])["author"]["tab_depth"] == 1


def test_tab_depth_survives_the_turn_author_parse_and_garbage_is_dropped():
    base = {"id": "bot:kepler", "name": "kepler", "is_bot": True}
    assert parse_turn_author({**base, "tab_depth": 2})["tab_depth"] == 2
    for junk in ("2", True, 0, -1, 2.5):
        assert "tab_depth" not in parse_turn_author({**base, "tab_depth": junk})


# ── the surface the model sees ───────────────────────────────────────────────


def test_the_schema_offers_new_tab():
    function = bot_mode_dm.message_agent_tool_schema()["function"]
    assert function["parameters"]["properties"]["new_tab"]["type"] == "boolean"
    assert "new_tab" in function["description"]


def test_the_inline_executor_passes_new_tab_through(monkeypatch):
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext

    seen = {}
    monkeypatch.setattr(bot_mode_dm, "message_agent_tool", lambda **kwargs: seen.update(kwargs) or "{}")
    INLINE_TOOL_EXECUTORS["message_agent"](object(), {"target": "me", "message": "brief", "new_tab": True},
                                           InlineToolContext(effective_task_id="t"))

    assert seen["new_tab"] is True and seen["target"] == "me"
