"""profiles.list ``side_windows``: the teammate's ``message_agent`` side windows, newest stamp first.

Why: a side window (a busy bot's DM opened in its own titled session) only showed up as one more
row in that bot's Sessions list, so the user never saw it. The desktop opens each one as a tab
beside the bot's Bot Chat; it learns about them from this field on the roster poll it already runs.
Only sessions the delivery runner stamped are listed: never plain CLI chats, crons or workers.
"""

from __future__ import annotations

import pytest

import tui_gateway.server as srv
from tui_gateway import profile_roster_cache


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    profile_roster_cache.invalidate()
    yield h
    profile_roster_cache.invalidate()


def _db(home):
    """A writable storage-interface handle on the tmp default profile's own ``state.db``."""
    from persistence_assembly import open_persistence, profile_scope

    return open_persistence(profile_scope(home=home), eager=True)


def _new(store, sid, title=None):
    from persistence.conversation.sessions import NewSession

    store.sessions.create(NewSession(session_id=sid, source="cli"))
    if title is not None:
        store.sessions.set_title(sid, title)


def _default_row(params=None):
    envelope = srv._methods["profiles.list"](1, params or {})
    return next(p for p in envelope["result"]["profiles"] if p["name"] == "default")


def test_side_windows_lists_only_stamped_sessions_newest_first(home):
    from persistence.conversation.messages import NewMessage

    db = _db(home)
    try:
        for sid, title in (("plain", "a terminal chat"), ("old", "older window"), ("new", "micro1 | Pricing")):
            _new(db, sid, title)
            db.messages.append(sid, NewMessage(role="user", content="hi"))
        db.sessions.mark_side_window("old", by="bot:atlas", at=1000.0)
        db.sessions.mark_side_window("new", by="bot:neo", at=2000.0)
    finally:
        db.close()

    windows = _default_row()["side_windows"]

    assert [(w["id"], w["title"], w["by"], w["at"]) for w in windows] == [
        ("new", "micro1 | Pricing", "bot:neo", 2000.0),
        ("old", "older window", "bot:atlas", 1000.0),
    ]


def test_side_windows_is_empty_without_any(home):
    db = _db(home)
    try:
        _new(db, "plain")
    finally:
        db.close()

    assert _default_row()["side_windows"] == []


def test_a_new_stamp_shows_on_the_very_next_poll(home):
    db = _db(home)
    try:
        _new(db, "w", "micro1")
    finally:
        db.close()
    assert _default_row()["side_windows"] == []

    db = _db(home)
    try:
        db.sessions.mark_side_window("w", by="bot:neo", at=3000.0)
    finally:
        db.close()

    # The roster memo keys on the store's signature, so the stamp write is never masked.
    assert [w["id"] for w in _default_row()["side_windows"]] == ["w"]
