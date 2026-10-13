"""The side-window marker: a ``message_agent`` side window is stamped in the teammate's state.db
so the desktop can open it as a tab, and nothing else ever is.

The delivery runner stamps the session the CLI transport is about to run (creating the titled
window first when it is new) on every DM turn, so a new DM into an existing window re-marks it.
The stamp lives in ``sessions.origin_json`` under ``side_window`` and survives compression; the
``"<title> (2)"`` retry window gets its own stamp. Plain CLI chats, crons and workers never carry it.
"""

import json
import subprocess
from pathlib import Path

import pytest

from hermes_state import SessionDB
from tools import bot_mode_dm, bot_mode_dm_side_window as side, bot_mode_probe, bot_relay

from tests.tools.test_bot_mode_dm import _managed_home

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


def _db(profile_home: Path) -> SessionDB:
    return SessionDB(db_path=profile_home / "state.db")


def _windows(profile_home: Path) -> list[dict]:
    if not (profile_home / "state.db").exists():
        return []
    db = _db(profile_home)
    try:
        return db.list_side_windows()
    finally:
        db.close()


def _side_argv(title="micro1"):
    return ["hermes", "-p", "researcher", "chat", "--in", "~", "-c", title, "--create-if-missing", "-Q"]


# ── the stamp ────────────────────────────────────────────────────────────────


def test_a_new_window_is_created_titled_and_marked(target):
    session_id = side.stamp_side_window(target, "micro1 | Pricing", author=AUTHOR)

    db = _db(target)
    try:
        row = db.get_session_by_title("micro1 | Pricing")
        assert row["id"] == session_id
        # The same shape `chat -c <title> --create-if-missing` mints, so the CLI simply resumes it.
        assert row["source"] == "cli"
    finally:
        db.close()
    [window] = _windows(target)
    assert window["id"] == session_id
    assert window["title"] == "micro1 | Pricing"
    assert window["by"] == "bot:neo"
    assert window["at"] > 0


def test_a_new_dm_into_an_existing_window_restamps_it(target, monkeypatch):
    db = _db(target)
    try:
        db.create_session("existing", source="cli")
        db.set_session_title("existing", "micro1")
        db.append_message("existing", "user", "earlier DM")
    finally:
        db.close()

    monkeypatch.setattr(side.time, "time", lambda: 1000.0)
    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "existing"
    monkeypatch.setattr(side.time, "time", lambda: 2000.0)
    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "existing"

    assert [(w["id"], w["at"]) for w in _windows(target)] == [("existing", 2000.0)]


def test_the_stamp_lands_on_the_compression_tip_the_cli_resumes(target):
    db = _db(target)
    try:
        db.create_session("root", source="cli")
        db.set_session_title("root", "micro1")
        db.append_message("root", "user", "first DM")
        assert db.try_acquire_compression_lock("root", "w", ttl_seconds=60)
        db.publish_compression_child(parent_session_id="root", child_session_id="tip", source="cli",
                                     messages=[{"role": "user", "content": "summary"}],
                                     compression_lock_holder="w")
    finally:
        db.close()

    assert side.stamp_side_window(target, "micro1", author=AUTHOR) == "tip"

    assert [w["id"] for w in _windows(target)] == ["tip"]


def test_a_marked_lineage_lists_once_under_its_tip(target):
    side.stamp_side_window(target, "micro1", author=AUTHOR)
    db = _db(target)
    try:
        root = db.get_session_by_title("micro1")["id"]
        assert db.try_acquire_compression_lock(root, "w", ttl_seconds=60)
        db.publish_compression_child(parent_session_id=root, child_session_id="tip", source="cli",
                                     messages=[{"role": "user", "content": "summary"}],
                                     compression_lock_holder="w")
    finally:
        db.close()

    # The continuation inherits the stamp; the window is still ONE tab, keyed by the live tip.
    assert [w["id"] for w in _windows(target)] == ["tip"]


def test_plain_cli_chats_never_list(target):
    db = _db(target)
    try:
        db.create_session("plain", source="cli")
        db.set_session_title("plain", "a terminal chat")
        db.create_session("cron1", source="cron")
    finally:
        db.close()
    side.stamp_side_window(target, "micro1", author=AUTHOR)

    assert [w["title"] for w in _windows(target)] == ["micro1"]


def test_a_gateway_routing_origin_is_never_overwritten(target):
    origin = json.dumps({"platform": "telegram", "chat_id": "42"})
    db = _db(target)
    try:
        db.create_session("tg", source="telegram", origin_json=origin)
        db.set_session_title("tg", "micro1")
    finally:
        db.close()

    side.stamp_side_window(target, "micro1", author=AUTHOR)

    db = _db(target)
    try:
        assert db.get_session("tg")["origin_json"] == origin
    finally:
        db.close()
    assert _windows(target) == []


def test_a_stamp_failure_never_raises(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")

    assert side.stamp_side_window(blocker, "micro1", author=AUTHOR) is None


# ── the runner stamps every side-window turn, before the transport runs ──────


def _fake_run(monkeypatch, target, *responses):
    """subprocess.run stand-in that records, per call, the windows already stamped AT that moment."""
    seen = []
    queue = list(responses)

    def fake_run(argv, **kwargs):
        seen.append(sorted(w["title"] for w in _windows(target)))
        return queue.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _dm(tmp_path):
    dm_file = tmp_path / "dm.txt"
    dm_file.write_text("hi", encoding="utf-8")
    return str(dm_file)


def test_a_side_window_turn_is_stamped_before_its_transport_runs(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    seen = _fake_run(monkeypatch, target, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))

    code = bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False,
                                     profile_home=target, author=AUTHOR, side_window=True)

    assert code == 0
    assert seen == [["micro1"]]
    assert _windows(target)[0]["by"] == "bot:neo"


def test_the_numbered_retry_window_gets_its_own_stamp(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    not_owned = subprocess.CompletedProcess([], 1, stdout="", stderr="hermes-refusal-reason: SESSION_NOT_OWNED\n")
    seen = _fake_run(monkeypatch, target, not_owned, subprocess.CompletedProcess([], 0, stdout="R", stderr=""))

    bot_mode_dm._run_delivery(_side_argv("micro1"), _dm(tmp_path), stdin_file=False,
                              profile_home=target, author=AUTHOR, side_window=True)

    assert seen == [["micro1"], ["micro1", "micro1 (2)"]]


def test_a_bot_chat_turn_is_never_stamped(tmp_path, target, monkeypatch):
    monkeypatch.setattr(bot_mode_dm, "_default_home", lambda: str(target.parent.parent))
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm", lambda *a, **k: None)
    seen = _fake_run(monkeypatch, target, subprocess.CompletedProcess([], 0, stdout="REPLY", stderr=""))

    bot_mode_dm._run_local_turn(["hermes", "-p", "researcher", *bot_relay.BOT_CHAT_TURN_ARGS], _dm(tmp_path),
                                profile_home=target, author=AUTHOR)

    assert seen == [[]]
    assert _windows(target) == []
