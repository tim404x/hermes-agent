"""``SessionQuery.recent_candidates`` (port of #151737's bounded roster query, kept exact): a recency-ranked first
page ranked over a window of recently active rows equals the full ranking's page, whatever the store looks like.
Fork carry, desktop speed pass (add45)."""

import dataclasses
import random
import sqlite3
import time
from contextlib import closing

import pytest

from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import (INTERNAL_LISTING_SOURCES, NewSession, SessionOrder, SessionQuery,
                                               SessionWindow)
from persistence.sqlite.conversation._store import listing_memo
from persistence.sqlite.conversation._store import sessions as store_sessions
from tests.persistence_sqlite.conftest import store_for

NOW = 1_790_000_000.0
SOURCES = ("desktop", "cli", "telegram", "cron", "oneshot", "kanban", "tool", "subagent")


@pytest.fixture(autouse=True)
def _no_memo(monkeypatch):
    monkeypatch.setenv("HERMES_LISTING_MEMO", "0")
    monkeypatch.delenv("HERMES_LISTING_WINDOW", raising=False)
    listing_memo.clear()


def _populate(store, scope, seed: int, n: int = 260):
    rnd = random.Random(seed)
    store.sessions.create(NewSession("first", "cli"))  # the store file exists from the first write on
    ids = []
    with closing(sqlite3.connect(store_for(scope))) as conn:
        for i in range(n):
            sid = f"s{i:04d}"
            started = NOW - rnd.uniform(0, 30 * 86400)
            kind = rnd.random()
            if kind < 0.15:
                last = None  # a pre-heartbeat row: its recency is its newest message
            elif kind < 0.18:
                last = -5.0  # a salvaged out-of-window cell
            else:
                last = started + rnd.uniform(0, 3 * 86400)
            conn.execute("INSERT INTO sessions(id, source, started_at, last_activity_at, message_count) "
                         "VALUES (?, ?, ?, ?, 0)", (sid, rnd.choice(SOURCES), started, last))
            ids.append(sid)
        conn.commit()
    # Messages far newer than their session's heartbeat (and some old ones), before the chains close their roots.
    for sid in rnd.sample(ids, 60):
        store.messages.append(sid, NewMessage(role="user", content=f"hi {sid}", timestamp=NOW - rnd.uniform(0, 4 * 86400)))
    with closing(sqlite3.connect(store_for(scope))) as conn:
        # Compression chains: an old root whose continuation is the freshest row of its chain.
        for i in range(0, n, 13):
            root, tip = ids[i], f"{ids[i]}-tip"
            conn.execute("UPDATE sessions SET end_reason = 'compression', ended_at = started_at + 10 WHERE id = ?", (root,))
            conn.execute("INSERT INTO sessions(id, source, parent_session_id, started_at, last_activity_at, message_count) "
                         "SELECT ?, CASE WHEN ? % 2 = 0 THEN source ELSE 'oneshot' END, id, started_at + 20, ?, 0 "
                         "FROM sessions WHERE id = ?", (tip, i, NOW - rnd.uniform(0, 5 * 86400), root))
        conn.commit()
    return ids


def _ids(entries):
    return [(str(e.session.session_id), e.last_active, e.preview) for e in entries]


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("limit", [1, 8, 20])
@pytest.mark.parametrize("excluded", [(), (*INTERNAL_LISTING_SOURCES, "cron")])
def test_the_windowed_page_is_the_full_rankings_page(backend, scope_a, seed, limit, excluded):
    store = backend.open(scope_a)
    _populate(store, scope_a, seed)
    query = SessionQuery(order=SessionOrder.LAST_ACTIVE, exclude_sources=excluded)
    full = store.sessions.list_sessions(query, SessionWindow(limit=limit), compact_rows=True)
    for window in (4, 16, 128, 2048):
        fast = store.sessions.list_sessions(dataclasses.replace(query, recent_candidates=window),
                                            SessionWindow(limit=limit), compact_rows=True)
        assert _ids(fast) == _ids(full)
        assert fast == full


def test_a_busy_store_is_ranked_over_the_window(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    _populate(store, scope_a, 7, n=400)
    used = []
    real = store_sessions._windowed_page

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs)
        used.append(rows is not None)
        return rows

    monkeypatch.setattr(store_sessions, "_windowed_page", spy)
    query = SessionQuery(order=SessionOrder.LAST_ACTIVE, recent_candidates=64)
    store.sessions.list_sessions(query, SessionWindow(limit=5), compact_rows=True)
    assert used == [True]


def test_a_message_newer_than_every_heartbeat_ranks_its_old_session_first(backend, scope_a):
    store = backend.open(scope_a)
    _populate(store, scope_a, 11)
    store.sessions.create(NewSession("ancient", "desktop"))
    with closing(sqlite3.connect(store_for(scope_a))) as conn:
        conn.execute("UPDATE sessions SET started_at = ?, last_activity_at = NULL WHERE id = 'ancient'", (NOW - 400 * 86400,))
        conn.commit()
    store.messages.append("ancient", NewMessage(role="user", content="back again", timestamp=time.time() + 3600))
    page = store.sessions.list_sessions(SessionQuery(order=SessionOrder.LAST_ACTIVE, recent_candidates=4),
                                        SessionWindow(limit=3), compact_rows=True)
    assert str(page[0].session.session_id) == "ancient"


def test_the_window_is_skipped_where_it_cannot_apply(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    _populate(store, scope_a, 5)
    calls = []
    monkeypatch.setattr(store_sessions, "_windowed_page", lambda *a, **k: calls.append(1))
    for query, window in (
        (SessionQuery(order=SessionOrder.LAST_ACTIVE, recent_candidates=8, sources=("cli",)), SessionWindow(limit=5)),
        (SessionQuery(order=SessionOrder.LAST_ACTIVE, recent_candidates=8, search_text="hi"), SessionWindow(limit=5)),
        (SessionQuery(order=SessionOrder.LAST_ACTIVE, recent_candidates=8), SessionWindow(limit=5, offset=5)),
        (SessionQuery(recent_candidates=8), SessionWindow(limit=5)),  # started_at order
        (SessionQuery(order=SessionOrder.LAST_ACTIVE), SessionWindow(limit=5)),  # no hint
    ):
        store.sessions.list_sessions(query, window, compact_rows=True)
    assert calls == []


def _spy_window(monkeypatch):
    used = []
    real = store_sessions._windowed_page

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs)
        used.append(rows is not None)
        return rows

    monkeypatch.setattr(store_sessions, "_windowed_page", spy)
    return used


def test_worker_rows_starting_newest_do_not_push_a_filtered_page_out_of_the_window(backend, scope_a, monkeypatch):
    # A worker-heavy store: the newest starts are all one-shot rows the query excludes; its own rows are days old.
    store = backend.open(scope_a)
    store.sessions.create(NewSession("first", "cli"))
    with closing(sqlite3.connect(store_for(scope_a))) as conn:
        for i in range(40):
            t = NOW - 10 * 86400 - i * 600
            conn.execute("INSERT INTO sessions(id, source, started_at, last_activity_at, message_count) "
                         "VALUES (?, 'desktop', ?, ?, 0)", (f"d{i}", t, t + 60))
        for i in range(400):
            t = NOW - i * 30
            conn.execute("INSERT INTO sessions(id, source, started_at, last_activity_at, message_count) "
                         "VALUES (?, 'oneshot', ?, ?, 0)", (f"w{i}", t, t + 5))
        conn.commit()
    used = _spy_window(monkeypatch)
    query = SessionQuery(order=SessionOrder.LAST_ACTIVE, exclude_sources=INTERNAL_LISTING_SOURCES)
    full = store.sessions.list_sessions(query, SessionWindow(limit=8), compact_rows=True)
    fast = store.sessions.list_sessions(dataclasses.replace(query, recent_candidates=16), SessionWindow(limit=8),
                                        compact_rows=True)
    assert fast == full
    assert used == [True]


def test_a_chain_deeper_than_any_walk_cap_keeps_the_window(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    _populate(store, scope_a, 3)
    with closing(sqlite3.connect(store_for(scope_a))) as conn:
        parent = None
        for i in range(90):  # a long-lived Bot Chat compressed 90 times
            conn.execute("INSERT INTO sessions(id, source, parent_session_id, started_at, last_activity_at, end_reason,"
                         " message_count) VALUES (?, 'desktop', ?, ?, ?, ?, 0)",
                         (f"bc{i}", parent, NOW - 90 * 3600 + i * 3600, NOW - 90 * 3600 + i * 3600 + 60,
                          None if i == 89 else "compression"))
            parent = f"bc{i}"
        conn.commit()
    used = _spy_window(monkeypatch)
    query = SessionQuery(order=SessionOrder.LAST_ACTIVE)
    full = store.sessions.list_sessions(query, SessionWindow(limit=8), compact_rows=True)
    fast = store.sessions.list_sessions(dataclasses.replace(query, recent_candidates=16), SessionWindow(limit=8),
                                        compact_rows=True)
    assert fast == full
    assert used == [True]


def test_kill_switch(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    _populate(store, scope_a, 6)
    monkeypatch.setenv("HERMES_LISTING_WINDOW", "0")
    holder = store.sessions._store.holder()
    with holder._read_ctx() as conn:
        assert store_sessions._recent_window(conn, 8, None) is None
