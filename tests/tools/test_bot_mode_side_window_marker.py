"""The side-window marker: a ``message_agent`` side window is stamped in the teammate's state.db
so the desktop can open it as a tab, and nothing else ever is.

The delivery runner stamps the session the CLI transport is about to run (creating the titled
window first when it is new) on every DM turn, so a new DM into an existing window re-marks it.
The stamp lives in ``sessions.origin_json`` under ``side_window`` and survives compression; the
``"<title> (2)"`` retry window gets its own stamp. Plain CLI chats, crons and workers never carry it.
"""

import json
import subprocess
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


def test_a_gateway_routing_origin_is_never_overwritten(target):
    origin = json.dumps({"platform": "telegram", "chat_id": "42"})
    with _db(target) as store:
        _new_session(store, "tg", source="telegram", title="micro1", origin=json.loads(origin))

    side.stamp_side_window(target, "micro1", author=AUTHOR)

    with _db(target) as store:
        assert store.sessions.get("tg").origin_json == origin
    assert _windows(target) == []


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
