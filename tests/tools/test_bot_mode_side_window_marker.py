"""The side-window marker: a ``message_agent`` side window is stamped in the teammate's state.db
so the desktop can open it as a tab, and nothing else ever is.

The delivery runner stamps the session the CLI transport is about to run (creating the titled
window first when it is new) on every DM turn, so a new DM into an existing window re-marks it.
The stamp lives in its own ``sessions.side_window_json`` cell (never ``origin_json``) and survives
compression; the ``"<title> (2)"`` retry window gets its own stamp. Plain CLI chats, crons and workers
never carry it.
"""

import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe

from tests.tools.test_bot_mode_dm import _append, _managed_home, _new_session, _profile_store

AUTHOR = {"id": "bot:neo", "name": "Neo", "is_bot": True}


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


@pytest.fixture
def target(tmp_path):
    home = _managed_home(tmp_path, teammates=("researcher",))
    return home / "profiles" / "researcher"


def _db(profile_home: Path):
    """A writable storage-interface handle on the target's own (tmp) store."""
    return _profile_store(profile_home)


def _windows(profile_home: Path) -> list[dict]:
    if not (profile_home / "state.db").exists():
        return []
    from persistence_assembly import open_persistence_reader

    with open_persistence_reader(profile_home) as store:
        return [asdict(w) for w in store.sessions.list_side_windows()]


def _publish_compression_child(store, root: str, tip: str) -> None:
    """``root`` compressed into the continuation ``tip``, as the agent's compressor publishes it while
    holding the compression lock (``holder="w"``)."""
    from persistence import SessionId
    from persistence.conversation.lineage import CompressionChild
    from persistence.conversation.messages import NewMessage

    store.lineage.publish_compression_child(SessionId(root), CompressionChild(session_id=SessionId(tip), source="cli"),
                                            [NewMessage(role="user", content="summary")],
                                            compression_lock_holder="w")


def _side_argv(title="micro1"):
    return ["hermes", "-p", "researcher", "chat", "--in", "~", "-c", title, "--create-if-missing", "-Q"]


# ── the stamp ────────────────────────────────────────────────────────────────


def _window(profile_home: Path, sid: str, title: str, *, message: bool = True) -> None:
    """A window row as the gateway mints it on the transport's first turn (with its first DM)."""
    with _db(profile_home) as store:
        _new_session(store, sid, source="cli", title=title)
        if message:
            _append(store, sid, "user", "a DM")


def test_a_new_window_is_not_pre_minted(target):
    # The gateway (single writer) mints a new window through --create-if-missing; a row written
    # here directly is not gateway-owned and its turn fails with permission_denied (4 Nov 2026).
    assert side.stamp_side_window(target, "micro1 | Pricing", author=AUTHOR) is None
    with _db(target) as store:
        assert store.sessions.get_by_title("micro1 | Pricing") is None
    assert _windows(target) == []


def test_an_existing_window_is_marked(target):
    _window(target, "w1", "micro1 | Pricing")
    assert side.stamp_side_window(target, "micro1 | Pricing", author=AUTHOR) == "w1"
    [window] = _windows(target)
    assert (window["id"], window["title"], window["by"]) == ("w1", "micro1 | Pricing", "bot:neo")
    assert window["at"] > 0


def test_an_empty_premint_from_an_older_runner_is_dropped(target):
    _window(target, "old", "micro1", message=False)
    assert side.stamp_side_window(target, "micro1", author=AUTHOR) is None
    with _db(target) as store:
        assert store.sessions.get("old") is None


def test_a_new_dm_into_an_existing_window_restamps_it(target, monkeypatch):
    with _db(target) as store:
        _new_session(store, "existing", source="cli", title="micro1")
        _append(store, "existing", "user", "earlier DM")

    monkeypatch.setattr(side.time, "time", lambda: 1000.0)
    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "existing"
    monkeypatch.setattr(side.time, "time", lambda: 2000.0)
    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "existing"

    assert [(w["id"], w["at"]) for w in _windows(target)] == [("existing", 2000.0)]


def test_the_stamp_lands_on_the_compression_tip_the_cli_resumes(target):
    with _db(target) as store:
        _new_session(store, "root", source="cli", title="micro1")
        _append(store, "root", "user", "first DM")
        assert store.coordination.try_acquire_compression_lock(session_id="root", holder="w", ttl_seconds=60)
        _publish_compression_child(store, "root", "tip")

    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "tip"

    assert [w["id"] for w in _windows(target)] == ["tip"]


def test_a_marked_lineage_lists_once_under_its_tip(target):
    _window(target, "w1", "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR)
    with _db(target) as store:
        root = store.sessions.get_by_title("micro1").session_id
        assert store.coordination.try_acquire_compression_lock(session_id=root, holder="w", ttl_seconds=60)
        _publish_compression_child(store, root, "tip")

    # The continuation inherits the stamp; the window is still ONE tab, keyed by the live tip.
    assert [w["id"] for w in _windows(target)] == ["tip"]


def test_plain_cli_chats_never_list(target):
    with _db(target) as store:
        _new_session(store, "plain", source="cli", title="a terminal chat")
        _new_session(store, "cron1", source="cron")
    _window(target, "w1", "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR)

    assert [w["title"] for w in _windows(target)] == ["micro1"]


def test_a_gateway_created_window_is_stamped_and_its_routing_origin_never_overwritten(target):
    """Regression (P3b): a gateway-created chat (every desktop chat, Neo's own task tabs included) carries a
    routing origin; the stamp used to refuse it, so the window never became a tab."""
    origin = json.dumps({"platform": "local", "chat_id": "local-42", "user_id": "uid:1"})
    with _db(target) as store:
        _new_session(store, "gw", source="local", title="micro1", origin=json.loads(origin))

    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "gw"

    with _db(target) as store:
        assert store.sessions.get("gw").origin_json == origin
    assert [(w["id"], w["by"]) for w in _windows(target)] == [("gw", "bot:neo")]


def test_a_stamp_failure_never_raises(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    assert side.stamp_side_window(blocker, "micro1", author=AUTHOR) is None


# ── the runner stamps every side-window turn, before the transport runs ──────


def _fake_run(monkeypatch, target, *responses):
    """subprocess.run stand-in that records, per call, the windows already stamped AT that moment,
    then mints the transport's titled row the way the gateway's --create-if-missing does."""
    seen = []
    queue = list(responses)

    def fake_run(argv, **kwargs):
        seen.append(sorted(w["title"] for w in _windows(target)))
        title = argv[argv.index("-c") + 1] if "-c" in argv else None
        if title:
            with _db(target) as store:
                if store.sessions.get_by_title(title) is None:
                    sid = "gw-" + str(len(seen))
                    _new_session(store, sid, source="cli", title=title)
                    _append(store, sid, "user", "hi")
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _dm(tmp_path):
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")
    return str(dm_file)


def test_an_existing_window_is_stamped_before_its_transport_runs(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _window(target, "w1", "micro1")
    seen = _fake_run(monkeypatch, target, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False,
                                     profile_home=target, author=AUTHOR, side_window=True)

    assert code == 0
    assert seen == [["micro1"]]
    assert _windows(target)[0]["by"] == "bot:neo"


def test_a_new_window_is_stamped_right_after_the_gateway_mints_it(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    seen = _fake_run(monkeypatch, target, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False,
                                     profile_home=target, author=AUTHOR, side_window=True)

    assert code == 0
    assert seen == [[]]
    assert [w["title"] for w in _windows(target)] == ["micro1"]


def test_the_numbered_retry_window_gets_its_own_stamp(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _window(target, "w1", "micro1")
    not_owned = subprocess.CompletedProcess([], 1, stdout="", stderr="hermes-refusal-reason: SESSION_NOT_OWNED\n")
    seen = _fake_run(monkeypatch, target, not_owned, subprocess.CompletedProcess([], 0, stdout="R", stderr=""))

    bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False,
                              profile_home=target, author=AUTHOR, side_window=True)

    assert seen == [["micro1"], ["micro1"]]
    assert sorted(w["title"] for w in _windows(target)) == ["micro1", "micro1 (2)"]


def test_a_bot_chat_turn_is_never_stamped(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm", lambda *a, **k: None)
    seen = _fake_run(monkeypatch, target, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))

    # The canonical Bot Chat turn argv (upstream retired its BOT_CHAT_TURN_ARGS constant in 8c46622dbb).
    bot_chat_turn = ("chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q")
    bot_mode_dm._run_local_turn(["hermes", "-p", "researcher", *bot_chat_turn], _dm(tmp_path),
                                profile_home=target, author=AUTHOR)

    assert seen == [[]]
    assert _windows(target) == []


# ── a window the desktop holds live gets the DM inside it, never a "(2)" sibling (P5) ─────────


KEY = "bot:neo>researcher|topic:micro1"


def _idle_window(profile_home: Path, sid: str, title: str) -> None:
    """A window whose last turn finished (its DM answered), so it is not busy."""
    _window(profile_home, sid, title)
    with _db(profile_home) as store:
        _append(store, sid, "assistant", "answered")


def _ready_authority(monkeypatch):
    """The target profile's gateway is up (its owner holds the window live in the app); its
    ``bot_relay.deliver`` door is the fake authority the Bot Chat tests use."""
    from types import SimpleNamespace

    from hermes_cli import gateway_runtime
    from tools import bot_live_delivery as live
    from tests.tools.test_bot_mode_dm import _FakeAuthority

    monkeypatch.setattr(gateway_runtime, "discover_gateway_endpoint", lambda home, timeout=5: SimpleNamespace(
        state="ready", endpoint=SimpleNamespace(instance_id="authority-1")))
    authority = _FakeAuthority()
    monkeypatch.setattr(live, "authority_delivery", authority)
    return authority


def _transport(monkeypatch, *responses):
    """subprocess.run stand-in recording each transport's window title."""
    titles, queue = [], list(responses)

    def fake_run(argv, **kwargs):
        titles.append(argv[argv.index("-c") + 1])
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return titles


_NOT_OWNED = subprocess.CompletedProcess([], 1, stdout="", stderr="hermes-refusal-reason: SESSION_NOT_OWNED\n")


def test_a_window_the_desktop_holds_live_is_admitted_through_its_owner(tmp_path, target, monkeypatch):
    """Regression (P5, 19 Nov 2026): watching a task tab spawned "Sales | Deal heatmap (2)" then "(3)"."""
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _idle_window(target, "w1", "micro1")
    assert side.stamp_side_window(target, "micro1", author=AUTHOR, key=KEY) == "w1"
    authority = _ready_authority(monkeypatch)
    waited = []
    monkeypatch.setattr(bot_mode_dm, "_wait_live_dm", lambda home, delivery_id, dm_file=None:
                        waited.append((home, delivery_id)) or 0)
    titles = _transport(monkeypatch, _NOT_OWNED)
    dm_file = _dm(tmp_path)

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), dm_file, stdin_file=False, profile_home=target,
                                     author=AUTHOR, side_window=True, window_key=KEY)

    assert code == 0
    assert titles == ["micro1"]  # the refused transport only: no "(2)" turn ever ran
    [(home, params)] = authority.calls
    assert home == target.resolve()
    assert (params["title"], params["session_id"], params["message"]) == ("micro1", "w1", "hi")
    assert params["author"] == AUTHOR
    assert waited == [(str(target.resolve()), params["id"])]
    assert [w["title"] for w in _windows(target)] == ["micro1"]


def test_another_conversations_window_with_the_title_still_retries_numbered(tmp_path, target, monkeypatch):
    """A real title collision (the titled chat belongs to another conversation) keeps the "(2)" retry."""
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _idle_window(target, "w1", "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR, key="bot:atlas>researcher|topic:micro1")
    authority = _ready_authority(monkeypatch)
    titles = _transport(monkeypatch, _NOT_OWNED, subprocess.CompletedProcess([], 0, stdout="R", stderr=""))

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False, profile_home=target,
                                     author=AUTHOR, side_window=True, window_key=KEY)

    assert code == 0
    assert titles == ["micro1", "micro1 (2)"]
    assert authority.calls == []


def test_an_owner_that_cannot_admit_falls_back_to_the_numbered_retry(tmp_path, target, monkeypatch):
    """No live owner reachable, nothing admitted: the DM still lands (in the old "(2)" window)."""
    from tools import bot_live_delivery as live

    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _idle_window(target, "w1", "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR, key=KEY)

    def unavailable(home, title=None):
        raise ValueError("profile authority is not ready")

    monkeypatch.setattr(live, "find_canonical_live_owner", unavailable)
    titles = _transport(monkeypatch, _NOT_OWNED, subprocess.CompletedProcess([], 0, stdout="R", stderr=""))
    dm_file = _dm(tmp_path)

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), dm_file, stdin_file=False, profile_home=target,
                                     author=AUTHOR, side_window=True, window_key=KEY)

    assert code == 0
    assert titles == ["micro1", "micro1 (2)"]
    assert not Path(bot_mode_dm._live_intent_file(dm_file)).exists()


def test_the_door_admits_into_a_stamped_side_window_only(target):
    """The owner's ``bot_relay.deliver`` door accepts a side window message_agent stamped, and still
    no other titled chat of the profile."""
    from tools import bot_live_delivery as live

    _window(target, "w1", "micro1")
    _window(target, "p1", "Tim's private chat")
    assert not live.door_accepts_title(target, "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR, key=KEY)
    assert live.door_accepts_title(target, "micro1")
    assert not live.door_accepts_title(target, "Tim's private chat")
    assert not live.door_accepts_title(target, "no such chat")


def test_a_busy_window_of_the_same_conversation_queues_the_dm_through_its_owner(tmp_path, target, monkeypatch):
    """Regression (P5): Kepler's "(2)" tab opened 9 s after the first DM because the first turn was still
    running. A follow-up into the conversation's own busy window is admitted into it (the owner queues it
    behind the running turn); no transport runs and no sibling opens."""
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    _window(target, "w1", "micro1")
    side.stamp_side_window(target, "micro1", author=AUTHOR, key=KEY)
    with _db(target) as store:
        _append(store, "w1", "user", "the first DM, still being answered", timestamp=time.time())
    authority = _ready_authority(monkeypatch)
    monkeypatch.setattr(bot_mode_dm, "_wait_live_dm", lambda home, delivery_id, dm_file=None: 0)
    titles = _transport(monkeypatch)

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False, profile_home=target,
                                     author=AUTHOR, side_window=True, window_key=KEY)

    assert code == 0
    assert titles == []
    [(_, params)] = authority.calls
    assert (params["title"], params["session_id"]) == ("micro1", "w1")
    assert [w["title"] for w in _windows(target)] == ["micro1"]
