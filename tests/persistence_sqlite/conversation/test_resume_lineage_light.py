"""Transcript pages keep the resume lineage without loading whole parent sessions."""

import sqlite3
from contextlib import closing, contextmanager
from itertools import pairwise
from unittest.mock import patch

import pytest

from persistence.sqlite.conversation._store import sessions
from persistence.sqlite.conversation._store.compression import _is_compression_child_row
from persistence.sqlite.conversation._store.messages import _is_explicit_fork_child_row, _resume_lineage_ids


@pytest.fixture
def holder(backend, scope_a):
    return backend.open(scope_a).holder()


def _old_resume_lineage_ids(holder, session_id):
    if not session_id:
        return [session_id]
    current = sessions.get_session(holder, session_id)
    if not current or _is_explicit_fork_child_row(holder, current):
        return [session_id]
    chain = [session_id]
    seen = {session_id}
    while len(chain) < 100:
        if not _is_compression_child_row(holder, current):
            break
        parent = sessions.get_session(holder, current["parent_session_id"])
        if not parent or parent["id"] in seen:
            break
        seen.add(parent["id"])
        chain.append(parent["id"])
        current = parent
    return list(reversed(chain))


def _compression_chain(holder, hops):
    ids = [f"segment-{index}" for index in range(hops + 1)]
    sessions.create_session(holder, ids[0], "cli")
    for parent, child in pairwise(ids):
        sessions.end_session(holder, parent, "compression")
        sessions.create_session(holder, child, "cli", parent_session_id=parent)
    return ids


def _assert_lineage(holder, session_id, expected):
    assert _resume_lineage_ids(holder, session_id) == _old_resume_lineage_ids(holder, session_id) == expected


@pytest.mark.parametrize("hops", [0, 30, 105])
def test_compression_lineage(holder, hops):
    ids = _compression_chain(holder, hops)
    _assert_lineage(holder, ids[-1], ids[-100:])


@pytest.mark.parametrize("marker", ["_branched_from", "_delegate_from", "_reset_from", "tool"])
def test_fork_breaks_lineage_but_its_own_continuation_resumes(holder, marker):
    sessions.create_session(holder, "root", "cli")
    sessions.end_session(holder, "root", "compression")
    config = None if marker == "tool" else {marker: "root"}
    sessions.create_session(holder, "fork", "tool" if marker == "tool" else "cli",
                            parent_session_id="root", model_config=config)
    sessions.end_session(holder, "fork", "compression")
    sessions.create_session(holder, "tip", "cli", parent_session_id="fork", model_config=config)
    _assert_lineage(holder, "fork", ["fork"])
    _assert_lineage(holder, "tip", ["fork", "tip"])


def test_noncompression_parent(holder):
    sessions.create_session(holder, "root", "cli")
    sessions.end_session(holder, "root", "branched")
    sessions.create_session(holder, "tip", "cli", parent_session_id="root")
    _assert_lineage(holder, "tip", ["tip"])


def test_missing_session_or_parent(holder):
    sessions.create_session(holder, "orphan", "cli")
    with closing(sqlite3.connect(holder.db_path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE sessions SET parent_session_id = 'missing' WHERE id = 'orphan'")
    for session_id in ("orphan", "missing", ""):
        _assert_lineage(holder, session_id, [session_id])


def test_cycle_stops_before_repeating_a_session(holder):
    first, second = _compression_chain(holder, 1)
    sessions.end_session(holder, second, "compression")
    holder._write_sql("UPDATE sessions SET parent_session_id = ? WHERE id = ?", (second, first))
    _assert_lineage(holder, first, [second, first])
    _assert_lineage(holder, second, [first, second])


@contextmanager
def _traced_reads(holder):
    statements, columns = [], set()
    read_ctx = holder._read_ctx

    def record_column(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ:
            columns.add((table, column))
        return sqlite3.SQLITE_OK

    @contextmanager
    def trace(*args, **kwargs):
        with read_ctx(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            conn.set_authorizer(record_column)
            try:
                yield conn
            finally:
                conn.set_authorizer(None)
                conn.set_trace_callback(None)

    with patch.object(holder, "_read_ctx", trace):
        yield statements, columns


def test_resume_lineage_read_budget(holder):
    hops = 30
    ids = _compression_chain(holder, hops)
    with (
        _traced_reads(holder) as (statements, columns),
        patch.object(sessions, "get_session", wraps=sessions.get_session) as full_read,
        patch.object(holder, "flush_for_read", wraps=holder.flush_for_read) as flush,
    ):
        assert _resume_lineage_ids(holder, ids[-1]) == ids
    assert full_read.call_count == 0
    flush.assert_called_once_with()
    assert len(statements) == 1
    assert not any(table == "system_prompts" or column == "system_prompt" for table, column in columns)


def test_transcript_id_lookup_reads_only_ids(holder):
    for session_id in ("chat", "chat-other"):
        sessions.create_session(holder, session_id, "cli")
    with _traced_reads(holder) as (_, columns):
        assert sessions.resolve_session_id(holder, "chat") == "chat"
    assert columns == {("sessions", "id")}
    assert sessions.resolve_session_id(holder, "chat-o") == "chat-other"
    assert sessions.resolve_session_id(holder, "cha") is None
