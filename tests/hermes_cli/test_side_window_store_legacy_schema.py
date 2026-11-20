"""Side-window store on a profile whose ``state.db`` predates ``sessions.side_window_json``, plus the stamp's
``origin_json`` mirror, its ``dm`` flag and the canonical Bot Chat refusal.

Regression (R3, 20 Nov 2026): every side-window reader and the compact session listing named the
``side_window_json`` column unconditionally, so a read-only reader (``open_persistence_reader`` never migrates)
on another profile's store raised ``no such column: side_window_json`` until that profile's first new-code write.
"""

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _store(home):
    from persistence_assembly import open_persistence, profile_scope

    return open_persistence(profile_scope(home=home), eager=True)


def _create(store, sid, *, title=None, origin=None, parent=None):
    from persistence import SessionId
    from persistence.conversation.sessions import NewSession

    store.sessions.create(NewSession(session_id=SessionId(sid), source="local", origin=origin,
                                     parent_session_id=SessionId(parent) if parent else None))
    if title:
        store.sessions.set_title(SessionId(sid), title)


def _seed(home, sid, *, title=None, origin=None):
    with _store(home) as store:
        _create(store, sid, title=title, origin=origin)


def _drop_side_window_column(home):
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN side_window_json")


def _cells(home, sid):
    with sqlite3.connect(home / "state.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    own = row["side_window_json"] if "side_window_json" in row.keys() else None
    return (json.loads(own) if own else None), (json.loads(row["origin_json"]) if row["origin_json"] else None)


# -- read-only readers on the previous schema --------------------------------------------------------------------

def test_readonly_profile_still_lists_legacy_tabs_before_first_write(home):
    from persistence_assembly import open_persistence_reader

    _seed(home, "old-window", title="Vendor", origin={"side_window": {"at": 1000, "by": "bot:default"}})
    _drop_side_window_column(home)
    with open_persistence_reader(home) as reader:
        assert [window.id for window in reader.sessions.list_side_windows()] == ["old-window"]


def test_readonly_reader_finds_legacy_stamps_by_key_on_an_unmigrated_store(home):
    from persistence_assembly import open_persistence_reader

    _seed(home, "s1")
    _seed(home, "keyed", title="Deal room", origin={"side_window": {"at": 2000, "by": "bot:neo", "key": "k"}})
    _drop_side_window_column(home)
    with open_persistence_reader(home) as reader:
        assert [(w.id, w.title) for w in reader.sessions.find_side_windows("k")] == [("keyed", "Deal room")]
        assert reader.sessions.find_side_windows("other") == ()
        assert [w.id for w in reader.sessions.list_side_windows()] == ["keyed"]
        assert reader.sessions.get("s1").side_window_json is None


def test_compact_listing_reads_an_unmigrated_store(home):
    from persistence.conversation.sessions import SessionQuery, SessionWindow
    from persistence_assembly import open_persistence_reader

    _seed(home, "plain", title="Plain chat")
    _seed(home, "legacy-tab", title="Legacy tab", origin={"side_window": {"at": 3000, "by": "bot:neo"}})
    _drop_side_window_column(home)
    with open_persistence_reader(home) as reader:
        rows = reader.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10), compact_rows=True)
        assert {row.session.session_id for row in rows} == {"plain", "legacy-tab"}
        assert all(row.session.side_window_json is None for row in rows)


def test_writer_on_the_previous_schema_stamps_the_origin_and_never_raises(home):
    gateway = {"platform": "telegram", "chat_id": "c1", "user_id": "u1"}
    with _store(home) as store:
        _create(store, "empty")
        _create(store, "routed", origin=gateway)
        _drop_side_window_column(home)
        assert store.sessions.mark_side_window("empty", by="bot:neo", at=10.0, key="k", dm=True) is True
        assert store.sessions.mark_side_window("routed", by="bot:neo", at=11.0) is False
        assert [(w.id, w.at) for w in store.sessions.list_side_windows()] == [("empty", 10.0)]
    assert _cells(home, "empty") == (None, {"side_window": {"at": 10.0, "by": "bot:neo", "key": "k", "dm": True}})
    assert _cells(home, "routed") == (None, gateway)


# -- origin_json mirror (rollback safety) ------------------------------------------------------------------------

def test_stamp_is_mirrored_into_an_empty_origin_but_never_over_a_gateway_origin(home):
    gateway = {"platform": "local", "chat_id": "local-g", "user_id": "uid:1"}
    with _store(home) as store:
        _create(store, "empty")
        _create(store, "routed", origin=gateway)
        _create(store, "legacy", origin={"side_window": {"at": 1.0, "by": "old", "key": "kk"}})
        assert store.sessions.mark_side_window("empty", by="bot:neo", at=5.0) is True
        assert store.sessions.mark_side_window("routed", by="bot:neo", at=6.0) is True
        assert store.sessions.mark_side_window("legacy", by="bot:neo", at=7.0) is True
    stamp = {"at": 5.0, "by": "bot:neo"}
    assert _cells(home, "empty") == (stamp, {"side_window": stamp})
    assert _cells(home, "routed") == ({"at": 6.0, "by": "bot:neo"}, gateway)
    kept = {"at": 7.0, "by": "bot:neo", "key": "kk"}
    assert _cells(home, "legacy") == (kept, {"side_window": kept})


# -- dm flag -----------------------------------------------------------------------------------------------------

def test_dm_flag_is_stamped_and_survives_a_plain_restamp(home):
    with _store(home) as store:
        _create(store, "dm-window")
        _create(store, "plain-window")
        _create(store, "legacy-dm", origin={"side_window": {"at": 1.0, "by": "old", "dm": True}})
        assert store.sessions.mark_side_window("dm-window", by="bot:neo", at=1.0, key="k", dm=True)
        assert store.sessions.mark_side_window("dm-window", by="bot:trinity", at=2.0)
        assert store.sessions.mark_side_window("plain-window", by="bot:neo", at=3.0)
        assert store.sessions.mark_side_window("legacy-dm", by="bot:neo", at=4.0)
    assert _cells(home, "dm-window")[0] == {"at": 2.0, "by": "bot:trinity", "key": "k", "dm": True}
    assert _cells(home, "plain-window")[0] == {"at": 3.0, "by": "bot:neo"}
    assert _cells(home, "legacy-dm")[0] == {"at": 4.0, "by": "bot:neo", "dm": True}


# -- canonical Bot Chat ------------------------------------------------------------------------------------------

def test_the_canonical_bot_chat_and_its_continuations_are_never_stamped(home):
    with _store(home) as store:
        _create(store, "bot-chat", title="Bot Chat")
        assert store.sessions.mark_side_window("bot-chat", by="bot:neo") is False
        # A compression continuation whose lineage root is the Bot Chat.
        with sqlite3.connect(home / "state.db") as conn:
            conn.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = 'bot-chat'")
        _create(store, "bot-chat-tip", parent="bot-chat")
        assert store.sessions.mark_side_window("bot-chat-tip", by="bot:neo") is False
        assert store.sessions.list_side_windows() == ()
    assert _cells(home, "bot-chat") == (None, None)
    assert _cells(home, "bot-chat-tip") == (None, None)
