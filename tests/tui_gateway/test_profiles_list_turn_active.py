"""``profiles.list`` ``turn_active``: is this bot ITSELF mid-turn right now?

Why: the desktop roster animated a bot's face off ``worker_session`` (any oneshot / kanban / tool
run under the profile), so Neo orbited all evening while background Outbox workers ran and its
chat sat idle. Meanwhile a bot genuinely answering in a non-focused chat showed nothing, because
the client's own "thinking" signal follows the focused tile only.

Contract under test:
- ``turn_active`` is True only while the canonical Bot Chat tip, or a recent human-facing session
  (a ``message_agent`` side window, a CLI/TUI chat), has an unfinished newest row: role user or
  tool, or an assistant row carrying tool_calls, less than 15 minutes old.
- ``turn_active_session`` names that session, else None.
- Background workers (oneshot / kanban / tool) and cron runs never count.
- The roster's per-store memo must not freeze the answer: it flips on the very next poll when a
  turn starts or ends, and a crashed turn ages out after 15 minutes even though the store never
  moves again.
- Read errors answer False, never fail the listing.
"""

from __future__ import annotations

import time

import pytest

import tui_gateway.server as srv
from hermes_state import SessionDB
from tui_gateway import profile_roster_cache as cache

_CALL = [{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]


@pytest.fixture(autouse=True)
def _clean_memo():
    cache.invalidate()
    yield
    cache.invalidate()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _session(home, sid, *rows, source="desktop", title=None):
    """A session holding ``rows`` = (role, tool_calls, age seconds)."""
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=sid, source=source)
        if title:
            db.set_session_title(sid, title)
        for role, tool_calls, age in rows:
            db.append_message(sid, role, content=f"{role} row", tool_calls=tool_calls,
                              tool_call_id="call_1" if role == "tool" else None,
                              timestamp=time.time() - age)
    finally:
        db.close()


def _append(home, sid, role, tool_calls=None, age=0):
    db = SessionDB(db_path=home / "state.db")
    try:
        db.append_message(sid, role, content=f"{role} row", tool_calls=tool_calls,
                          tool_call_id="call_1" if role == "tool" else None, timestamp=time.time() - age)
    finally:
        db.close()


def _row():
    envelope = srv._methods["profiles.list"](1, {})
    return next(p for p in envelope["result"]["profiles"] if p["name"] == "default")


def _turn(row):
    return row["turn_active"], row["turn_active_session"]


def test_no_store_is_idle(home):
    assert _turn(_row()) == (False, None)


def test_finished_bot_chat_is_idle(home):
    _session(home, "chat", ("user", None, 60), ("assistant", None, 50), title="Bot Chat")
    assert _turn(_row()) == (False, None)


@pytest.mark.parametrize("rows", [
    [("user", None, 5)],
    [("user", None, 30), ("assistant", _CALL, 20), ("tool", None, 10)],
    [("user", None, 30), ("assistant", _CALL, 20)],
], ids=["user", "tool", "assistant-tool-calls"])
def test_unfinished_fresh_bot_chat_is_active(home, rows):
    _session(home, "chat", *rows, title="Bot Chat")
    assert _turn(_row()) == (True, "chat")


def test_unfinished_tip_older_than_fifteen_minutes_is_a_crash_not_a_turn(home):
    _session(home, "chat", ("user", None, 16 * 60), title="Bot Chat")
    assert _turn(_row()) == (False, None)


@pytest.mark.parametrize("source", ["oneshot", "kanban", "tool", "cron"])
def test_background_runs_never_count(home, source):
    _session(home, "chat", ("user", None, 120), ("assistant", None, 110), title="Bot Chat")
    _session(home, "bg", ("user", None, 10), ("assistant", _CALL, 5), source=source,
             title="Outbox worker contract #890")

    row = _row()
    assert _turn(row) == (False, None)
    if source != "cron":  # the worker row is still reported; it just no longer means "busy turn"
        assert row["worker_session"]["id"] == "bg"


def test_side_window_mid_turn_is_active_while_bot_chat_idles(home):
    _session(home, "chat", ("user", None, 120), ("assistant", None, 110), title="Bot Chat")
    _session(home, "side", ("user", None, 20), ("assistant", _CALL, 10), source="cli",
             title="micro1 | Pricing + Volume")
    assert _turn(_row()) == (True, "side")


def test_an_old_finished_window_does_not_hide_a_busy_one(home):
    """Two side windows at once: the newer one finished, the older is still working."""
    _session(home, "chat", ("user", None, 300), ("assistant", None, 290), title="Bot Chat")
    _session(home, "busy", ("user", None, 60), ("assistant", _CALL, 40), source="cli", title="Atlas: kit counts")
    _session(home, "done", ("user", None, 30), ("assistant", None, 20), source="cli", title="Apex: SOW wording")
    assert _turn(_row()) == (True, "busy")


def test_memo_flips_both_ways_on_the_next_poll(home):
    _session(home, "chat", ("user", None, 5), title="Bot Chat")
    assert _turn(_row()) == (True, "chat")

    _append(home, "chat", "assistant")  # the turn ends: the final reply moves the store
    assert _turn(_row()) == (False, None)

    _append(home, "chat", "user")  # next turn starts
    assert _turn(_row()) == (True, "chat")


def test_memo_ages_a_crashed_turn_out_without_a_store_write(home, monkeypatch):
    _session(home, "chat", ("user", None, 10 * 60), title="Bot Chat")
    assert _turn(_row()) == (True, "chat")
    signature = cache.store_signature(home)

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 6 * 60)
    assert cache.store_signature(home) == signature  # nothing wrote: this poll is a memo hit
    assert _turn(_row()) == (False, None)


def test_unreadable_store_answers_idle(home):
    (home / "state.db").write_bytes(b"this is not a sqlite database" * 64)
    row = _row()
    assert _turn(row) == (False, None)
