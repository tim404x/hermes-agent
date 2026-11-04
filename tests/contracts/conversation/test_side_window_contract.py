"""Fork carry "bot-mode side windows" on the storage interface.

``message_agent`` opens a busy (or, by default, any) teammate's DM in a titled side window and
stamps it in the teammate's store so the desktop opens it as a tab and the conversation's next DM
finds it again (``tools/bot_mode_dm_side_window.py``). Ported from the old store's
``hermes_state_side_windows`` mixin when upstream removed it (be3d6affc8): the stamp lives in the
session's ``origin`` under ``SIDE_WINDOW_KEY`` and is written and read through
``SessionRepository.mark_side_window`` / ``list_side_windows`` / ``find_side_windows`` on every
backend.
"""

from __future__ import annotations

import json
import time

from persistence import SessionId
from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import (
    END_REASON_COMPRESSION,
    SIDE_WINDOW_KEY,
    NewSession,
    origin_is_side_window_only,
    side_window_stamp,
)

KEY = "bot:neo>researcher|topic:micro1"
# Message times inside the trusted epoch window, well in the past.
T = time.time() - 100_000


def _new(store, session_id: str, *, title: str | None = None, source: str = "cli", parent: str | None = None,
         origin: dict | None = None) -> None:
    store.sessions.create(NewSession(session_id=SessionId(session_id), source=source, origin=origin,
                                     parent_session_id=None if parent is None else SessionId(parent)))
    if title is not None:
        store.sessions.set_title(SessionId(session_id), title)


def _say(store, session_id: str, content: str, timestamp: float, role: str = "user") -> None:
    store.messages.append(SessionId(session_id), NewMessage(role=role, content=content, timestamp=timestamp))


def test_the_stamp_helpers_read_text_and_mappings():
    stamp = {"at": 1.0, "by": "bot:neo", "key": KEY}
    assert side_window_stamp({SIDE_WINDOW_KEY: stamp}) == stamp
    assert side_window_stamp(json.dumps({SIDE_WINDOW_KEY: stamp})) == stamp
    assert side_window_stamp(None) is None
    assert side_window_stamp("not json") is None
    assert side_window_stamp({"platform": "telegram"}) is None
    assert origin_is_side_window_only({SIDE_WINDOW_KEY: stamp}) is True
    assert origin_is_side_window_only(json.dumps({SIDE_WINDOW_KEY: stamp})) is True
    assert origin_is_side_window_only({SIDE_WINDOW_KEY: stamp, "platform": "telegram"}) is False
    assert origin_is_side_window_only(None) is False
    assert origin_is_side_window_only("not json") is False


def test_marked_rows_list_newest_stamp_first_and_plain_rows_never(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-plain", title="a terminal chat")
    _new(store, "sw-old", title="older window")
    _new(store, "sw-new", title="micro1 | Pricing")
    _say(store, "sw-new", "a DM", T + 1500)
    assert store.sessions.mark_side_window(SessionId("sw-old"), by="bot:atlas", at=1000.0) is True
    assert store.sessions.mark_side_window(SessionId("sw-new"), by="bot:neo", at=2000.0) is True

    windows = store.sessions.list_side_windows()

    assert [(w.id, w.title, w.by, w.at) for w in windows] == [
        ("sw-new", "micro1 | Pricing", "bot:neo", 2000.0),
        ("sw-old", "older window", "bot:atlas", 1000.0),
    ]
    assert windows[0].message_count == 1
    assert windows[1].message_count == 0
    assert windows[0].last_active > 0
    # CONTRACT: the stamp rides the row's origin, as the stored origin_json text.
    origin = store.sessions.get(SessionId("sw-new")).origin
    assert origin == {SIDE_WINDOW_KEY: {"at": 2000.0, "by": "bot:neo"}}


def test_a_stamp_defaults_to_now_and_keeps_its_key_when_restamped(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-k", title="micro1")
    before = time.time()
    assert store.sessions.mark_side_window(SessionId("sw-k"), by="bot:neo", key=KEY) is True
    stamp = side_window_stamp(store.sessions.get(SessionId("sw-k")).origin)
    assert before - 1 <= stamp["at"] <= time.time() + 1
    assert stamp["key"] == KEY
    assert store.sessions.mark_side_window(SessionId("sw-k"), by="bot:atlas", at=5.0) is True
    assert side_window_stamp(store.sessions.get(SessionId("sw-k")).origin) == {"at": 5.0, "by": "bot:atlas", "key": KEY}
    assert store.sessions.mark_side_window(SessionId("sw-k"), at=6.0, key="other") is True
    assert side_window_stamp(store.sessions.get(SessionId("sw-k")).origin) == {"at": 6.0, "by": "", "key": "other"}


def test_a_foreign_origin_or_a_missing_row_is_never_stamped(backend, scope_a):
    store = backend.open(scope_a)
    origin = {"platform": "telegram", "chat_id": "42"}
    _new(store, "sw-tg", title="micro1", source="telegram", origin=origin)

    assert store.sessions.mark_side_window(SessionId("sw-tg"), by="bot:neo") is False
    assert store.sessions.mark_side_window(SessionId("sw-missing"), by="bot:neo") is False

    assert store.sessions.get(SessionId("sw-tg")).origin == origin
    assert store.sessions.list_side_windows() == ()


def test_a_compressed_window_lists_once_under_its_live_tip(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-root", title="micro1")
    _say(store, "sw-root", "first DM", T + 100)
    assert store.sessions.mark_side_window(SessionId("sw-root"), by="bot:neo", at=T + 100, key=KEY) is True
    store.sessions.end(SessionId("sw-root"), END_REASON_COMPRESSION)
    _new(store, "sw-tip", parent="sw-root")
    _say(store, "sw-tip", "summary", T + 200)

    assert [w.id for w in store.sessions.list_side_windows()] == ["sw-tip"]
    [match] = store.sessions.find_side_windows(KEY)
    # The title a ``chat -c <title>`` transport resolves to the tip is the lineage root's.
    assert (match.id, match.title, match.at) == ("sw-tip", "micro1", T + 100)
    assert match.last_active >= T + 200


def test_find_reads_only_its_conversation_and_its_freshest_mark(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-a", title="micro1")
    _new(store, "sw-b", title="payroll")
    store.sessions.mark_side_window(SessionId("sw-a"), by="bot:neo", at=1000.0, key=KEY)
    store.sessions.mark_side_window(SessionId("sw-b"), by="bot:neo", at=2000.0, key="bot:neo>researcher|topic:payroll")
    _say(store, "sw-a", "a later DM", T + 3000)

    [match] = store.sessions.find_side_windows(KEY)

    assert (match.id, match.title, match.at) == ("sw-a", "micro1", 1000.0)
    assert match.last_active >= T + 3000
    assert store.sessions.find_side_windows("nobody>researcher|topic:none") == ()


def test_archived_windows_are_left_out_and_the_limit_holds(backend, scope_a):
    store = backend.open(scope_a)
    for n in range(4):
        _new(store, f"sw-{n}", title=f"window {n}")
        store.sessions.mark_side_window(SessionId(f"sw-{n}"), by="bot:neo", at=float(n + 1), key=KEY)
    store.sessions.set_archived(SessionId("sw-3"), True)

    assert [w.id for w in store.sessions.list_side_windows()] == ["sw-2", "sw-1", "sw-0"]
    assert [w.id for w in store.sessions.list_side_windows(limit=2)] == ["sw-2", "sw-1"]
    assert [w.id for w in store.sessions.find_side_windows(KEY, limit=1)] == ["sw-2"]
