"""Fork carry "bot-mode side windows" on the storage interface.

``message_agent`` opens a busy (or, by default, any) teammate's DM in a titled side window and
stamps it in the teammate's store so the desktop opens it as a tab and the conversation's next DM
finds it again (``tools/bot_mode_dm_side_window.py``). Ported from the old store's
``hermes_state_side_windows`` mixin when upstream removed it (be3d6affc8). The stamp lives in its own
cell (``SessionRecord.side_window_json``), never in the session's ``origin``, so any row can carry it,
a gateway-created chat included; a legacy stamp under ``origin[SIDE_WINDOW_KEY]`` still reads. It is
written and read through ``SessionRepository.mark_side_window`` / ``list_side_windows`` /
``find_side_windows`` on every backend.
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
    record_side_window_stamp,
    side_window_stamp,
)
from persistence.conversation.lineage import CompressionChild

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
    # CONTRACT: the stamp rides its own cell. While the cell is one release old an EMPTY origin also
    # carries a mirror of the stamp (rollback safety); a routing origin is never touched.
    record = store.sessions.get(SessionId("sw-new"))
    assert record.origin in (None, {"side_window": {"at": 2000.0, "by": "bot:neo"}})
    assert record_side_window_stamp(record) == {"at": 2000.0, "by": "bot:neo"}


def test_a_stamp_defaults_to_now_and_keeps_its_key_when_restamped(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-k", title="micro1")
    before = time.time()
    assert store.sessions.mark_side_window(SessionId("sw-k"), by="bot:neo", key=KEY) is True
    stamp = record_side_window_stamp(store.sessions.get(SessionId("sw-k")))
    assert before - 1 <= stamp["at"] <= time.time() + 1
    assert stamp["key"] == KEY
    assert store.sessions.mark_side_window(SessionId("sw-k"), by="bot:atlas", at=5.0) is True
    assert record_side_window_stamp(store.sessions.get(SessionId("sw-k"))) == {"at": 5.0, "by": "bot:atlas", "key": KEY}
    assert store.sessions.mark_side_window(SessionId("sw-k"), at=6.0, key="other") is True
    assert record_side_window_stamp(store.sessions.get(SessionId("sw-k"))) == {"at": 6.0, "by": "", "key": "other"}


def test_a_gateway_created_row_is_stamped_and_its_origin_kept_byte_identical(backend, scope_a):
    """Regression (P3b, 19 Nov 2026): every gateway-created chat carries a routing origin, Neo's own task tabs
    included, and the stamp used to refuse them all."""
    store = backend.open(scope_a)
    origin = {"platform": "local", "chat_id": "local-abc", "user_id": "uid:1"}
    _new(store, "sw-gw", title="Sales | Deal heatmap", source="local", origin=origin)
    stored = store.sessions.get(SessionId("sw-gw")).origin_json

    assert store.sessions.mark_side_window(SessionId("sw-gw"), by="bot:neo", at=3000.0, key=KEY) is True

    record = store.sessions.get(SessionId("sw-gw"))
    assert record.origin == origin
    assert record.origin_json == stored
    assert record_side_window_stamp(record) == {"at": 3000.0, "by": "bot:neo", "key": KEY}
    assert [(w.id, w.title, w.by, w.at) for w in store.sessions.list_side_windows()] == [
        ("sw-gw", "Sales | Deal heatmap", "bot:neo", 3000.0)]
    assert [m.id for m in store.sessions.find_side_windows(KEY)] == ["sw-gw"]
    # A restamp with a new key moves the conversation; the origin still never changes.
    assert store.sessions.mark_side_window(SessionId("sw-gw"), by="bot:kepler", at=3100.0, key="other") is True
    record = store.sessions.get(SessionId("sw-gw"))
    assert record.origin_json == stored
    assert record_side_window_stamp(record) == {"at": 3100.0, "by": "bot:kepler", "key": "other"}
    assert store.sessions.find_side_windows(KEY) == ()
    assert [m.id for m in store.sessions.find_side_windows("other")] == ["sw-gw"]


def test_a_missing_row_is_never_stamped(backend, scope_a):
    store = backend.open(scope_a)
    assert store.sessions.mark_side_window(SessionId("sw-missing"), by="bot:neo") is False
    assert store.sessions.list_side_windows() == ()


def test_a_legacy_origin_stamp_still_lists_and_finds(backend, scope_a):
    """Rows stamped before the stamp had its own cell (and the ones stamped by hand) keep working."""
    store = backend.open(scope_a)
    legacy = {SIDE_WINDOW_KEY: {"at": 4000.0, "by": "bot:neo", "key": KEY}}
    _new(store, "sw-legacy", title="Hardware | Rigs", origin=legacy)
    _new(store, "sw-plain", title="a terminal chat")

    assert [(w.id, w.by, w.at) for w in store.sessions.list_side_windows()] == [("sw-legacy", "bot:neo", 4000.0)]
    assert [m.id for m in store.sessions.find_side_windows(KEY)] == ["sw-legacy"]
    assert record_side_window_stamp(store.sessions.get(SessionId("sw-legacy"))) == legacy[SIDE_WINDOW_KEY]
    assert record_side_window_stamp(store.sessions.get(SessionId("sw-plain"))) is None
    # A new stamp on a legacy row wins over the old one.
    assert store.sessions.mark_side_window(SessionId("sw-legacy"), by="bot:atlas", at=4100.0) is True
    assert record_side_window_stamp(store.sessions.get(SessionId("sw-legacy"))) == {"at": 4100.0, "by": "bot:atlas", "key": KEY}
    assert [(w.id, w.at) for w in store.sessions.list_side_windows()] == [("sw-legacy", 4100.0)]


def test_a_compression_continuation_inherits_the_stamp(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "sw-p", title="Lab | Compute model", source="local", origin={"platform": "local", "chat_id": "c"})
    _say(store, "sw-p", "first DM", T + 100)
    assert store.sessions.mark_side_window(SessionId("sw-p"), by="bot:neo", at=T + 100, key=KEY) is True

    store.lineage.publish_compression_child(SessionId("sw-p"), CompressionChild(SessionId("sw-c"), "local"),
                                            [NewMessage(role="user", content="summary", timestamp=T + 200)],
                                            require_compression_lease=False)

    child = store.sessions.get(SessionId("sw-c"))
    assert record_side_window_stamp(child) == {"at": T + 100, "by": "bot:neo", "key": KEY}
    assert [w.id for w in store.sessions.list_side_windows()] == ["sw-c"]
    [match] = store.sessions.find_side_windows(KEY)
    assert (match.id, match.title) == ("sw-c", "Lab | Compute model")


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
