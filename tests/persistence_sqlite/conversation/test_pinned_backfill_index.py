"""The pinned back-fill every Desktop listing runs (``include_pinned``) seeks a partial index of the pins instead
of walking the whole table in ``started_at`` order."""

import sqlite3
from contextlib import closing

from persistence.conversation.sessions import NewSession, SessionOrder, SessionQuery, SessionWindow
from persistence.sqlite._holder import StoreHolder
from tests.persistence_sqlite.conftest import store_for


def _index_sql(path):
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_sessions_pinned'").fetchone()
    return row[0] if row else None


def test_a_new_store_has_the_pinned_index(backend, scope_a):
    backend.open(scope_a).sessions.create(NewSession("a", "cli"))
    sql = _index_sql(store_for(scope_a))
    assert sql is not None and "WHERE pinned = 1" in sql


def test_an_older_store_gains_the_pinned_index_on_its_next_writable_open(backend, scope_a, scope_b):
    from persistence.sqlite import SQLiteBackend

    backend.open(scope_a).sessions.create(NewSession("a", "cli"))
    backend.close()
    with closing(sqlite3.connect(store_for(scope_a))) as conn:
        conn.execute("DROP INDEX idx_sessions_pinned")
        conn.commit()
    assert _index_sql(store_for(scope_a)) is None
    reopened = SQLiteBackend({scope.hermes_home_key: store_for(scope) for scope in (scope_a, scope_b)})
    try:
        reopened.open(scope_a).sessions.list_sessions(SessionQuery(), SessionWindow(limit=1))
    finally:
        reopened.close()
    assert _index_sql(store_for(scope_a)) is not None


def test_the_listing_pinned_backfill_uses_the_partial_index(backend, scope_a, monkeypatch):
    store = backend.open(scope_a)
    for sid in ("a", "b", "c"):
        store.sessions.create(NewSession(sid, "cli"))
    store.sessions.set_pinned("a", True)
    caught = []
    real = StoreHolder._read_all

    def capture(self, sql, params=()):
        caught.append((sql, list(params)))
        return real(self, sql, params)

    monkeypatch.setattr(StoreHolder, "_read_all", capture)
    entries = store.sessions.list_sessions(
        SessionQuery(order=SessionOrder.LAST_ACTIVE, include_pinned=True), SessionWindow(limit=1))
    assert "a" in [str(e.session.session_id) for e in entries]
    backfill = [(sql, params) for sql, params in caught if "s.pinned = 1" in sql]
    assert backfill
    with closing(sqlite3.connect(store_for(scope_a))) as conn:
        plan = " ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + backfill[0][0], backfill[0][1]))
    assert "idx_sessions_pinned" in plan
