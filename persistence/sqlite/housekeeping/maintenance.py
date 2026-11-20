"""``SessionMaintenance`` on SQLite: the prune selection and delete, the ghost and orphan sweeps, the idle archive,
backend heartbeats, the export bound, the session projection digest and the retained-worktree reads, each one
transaction or read through the store holder. The export and import verbs come from ``SessionPortability``.

Post-commit effects of a delete (the delete hook, retired media, transcript files) are not run here: callers run
them over the ids ``prune_sessions`` and ``prune_empty_ghost_sessions`` return."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Sequence

from persistence.conversation.sessions import CANONICAL_BOT_CHAT_TITLE, INTERNAL_LISTING_SOURCES
from persistence.core.errors import ExportTooLarge
from persistence.core.identity import SessionId
from persistence.housekeeping.maintenance import (
    SESSION_COUNTER_FIELDS, SESSION_PROJECTION_FIELDS, PruneCandidate, PruneCriteria, PruneReclaimEstimate, PruneResult,
    SessionChangeDigests, TreeServeFacts,
)

from .._backend import SQLiteStore, register_repository, translated
from .._schema_ddl import _sql_json_extract, _sql_literal
from ..conversation._store.core import _delete_unreferenced_system_prompts
from ..conversation._store.retirement import retire_prunable
from ..conversation._store.sessions import _cwd_prefix_clause, _set_lineage_column_in_transaction
from ..conversation._store.sql_common import _non_continuation_child_sql
from ..conversation._store.write_guards import _guarded_ids, _write_guards_reject
from ..gateway._routes import _sql_session_last_active
from ._portability import SessionPortability
from ._retirement import prune_retired_tombstones

# concat_ws() is SQLite 3.44+; an older library keeps the per-row Python digest.
_CONCAT_WS_SQLITE = (3, 44, 0)

__all__ = ["SQLiteSessionMaintenance"]

# The prune and archive selection SQL.
_LAST_ACTIVE_SQL = _sql_session_last_active("s")
_TOKENS_SQL = "(COALESCE(s.input_tokens, 0) + COALESCE(s.output_tokens, 0))"
_COST_SQL = "COALESCE(s.actual_cost_usd, s.estimated_cost_usd, 0)"

# Ids per ``IN (?,...)`` list: SQLite caps bound parameters at SQLITE_MAX_VARIABLE_NUMBER (999 on builds
# < 3.32, 32766 after), and a bulk prune of a cron-heavy store binds tens of thousands of ids.
_SQL_IN_CHUNK = 900


def _id_chunks(ids, size: int = _SQL_IN_CHUNK):
    """Yield *ids* (any iterable) as lists of at most *size* elements."""
    ids = list(ids)
    for start in range(0, len(ids), size):
        yield ids[start:start + size]


def _placeholders(items) -> str:
    """``?,?,?`` for one bound parameter per element of *items* (a sequence or an int count)."""
    return ",".join("?" for _ in range(items if isinstance(items, int) else len(items)))


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards (``%``, ``_``) so derived text matches literally; pair with ``ESCAPE '\\'``."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(value: str) -> str:
    return f"%{_escape_like(value.lower())}%"


def _cwd_prefix_filter(value: str) -> tuple[list[str], list]:
    clause, params = _cwd_prefix_clause(value)
    return [clause], list(params)


def _one(clause: str, conv=None):
    return lambda v: ([clause], [conv(v) if conv else v])


# Prune/archive filters in evaluation order: (kwarg, applies-when, builder -> (clauses, params)).
# ``applies-when``: "notnone" (numeric/time bounds; 0 is a real bound) or "truthy" ("" = unset).
_PRUNE_FILTERS = (
    # Orphan-swept rows age from the sweep, not old activity, or the next prune deletes them before recovery.
    ("last_active_before", "notnone", lambda v: (
        [_LAST_ACTIVE_SQL + " < ?",
         "(COALESCE(s.end_reason, '') != 'startup_orphan_reap' OR s.ended_at < ?)"],
        [v, v])),
    ("last_active_after", "notnone", _one(_LAST_ACTIVE_SQL + " >= ?")),
    ("started_before", "notnone", _one("s.started_at < ?")),
    ("started_after", "notnone", _one("s.started_at >= ?")),
    ("source", "truthy", _one("s.source = ?")),
    ("title_like", "truthy", _one("LOWER(COALESCE(s.title, '')) LIKE ? ESCAPE '\\'", _like)),
    ("end_reason", "truthy", _one("s.end_reason = ?")),
    ("cwd_prefix", "truthy", _cwd_prefix_filter),
    ("min_messages", "notnone", _one("s.message_count >= ?")),
    ("max_messages", "notnone", _one("s.message_count <= ?")),
    ("model_like", "truthy", _one("LOWER(COALESCE(s.model, '')) LIKE ? ESCAPE '\\'", _like)),
    ("provider", "truthy", _one("LOWER(COALESCE(s.billing_provider, '')) = ?", str.lower)),
    ("user_id", "truthy", _one("s.user_id = ?")),
    ("chat_id", "truthy", _one("s.chat_id = ?")),
    ("chat_type", "truthy", _one("s.chat_type = ?")),
    ("branch_like", "truthy", _one("LOWER(COALESCE(s.git_branch, '')) LIKE ? ESCAPE '\\'", _like)),
    ("min_tokens", "notnone", _one(_TOKENS_SQL + " >= ?")),
    ("max_tokens", "notnone", _one(_TOKENS_SQL + " <= ?")),
    ("min_cost", "notnone", _one(_COST_SQL + " >= ?")),
    ("max_cost", "notnone", _one(_COST_SQL + " <= ?")),
    ("min_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) >= ?")),
    ("max_tool_calls", "notnone", _one("COALESCE(s.tool_call_count, 0) <= ?")),
)

# Child ``c`` continues compression-ended ``p`` (same predicate as compression's child lookup).
_CONTINUATION_EDGE_SQL = "p.end_reason = 'compression'\n" + _non_continuation_child_sql("c.", "p.id")


def _continued_ancestors_sql(candidates_where: str) -> str:
    """Compression ancestors of every row *candidates_where* (alias ``s``) does not select."""
    return ("WITH RECURSIVE kept(id) AS ("
            " SELECT p.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            f" AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id = c.id AND {candidates_where})"
            " UNION"
            " SELECT p.id FROM kept k JOIN sessions c ON c.id = k.id JOIN sessions p ON p.id = c.parent_session_id"
            f" WHERE {_CONTINUATION_EDGE_SQL}"
            ") SELECT id FROM kept")


# A pin covers the whole conversation, but a store can hold a pinned segment whose later
# continuations were published unpinned; those still belong to the pinned chat.
_PINNED_TAIL_SQL = ("WITH RECURSIVE tail(id) AS ("
                    " SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id"
                    f" WHERE COALESCE(p.pinned, 0) = 1 AND {_CONTINUATION_EDGE_SQL}"
                    " UNION"
                    " SELECT c.id FROM tail t JOIN sessions p ON p.id = t.id JOIN sessions c ON c.parent_session_id = p.id"
                    f" WHERE {_CONTINUATION_EDGE_SQL}"
                    ") SELECT id FROM tail")


def _not_pinned_sql(alias: str = "s") -> str:
    """Predicate sparing pinned rows and the unpinned continuations a pinned segment covers."""
    return f"COALESCE({alias}.pinned, 0) = 0 AND {alias}.id NOT IN ({_PINNED_TAIL_SQL})"


_PRUNE_FILTER_NAMES = frozenset(name for name, _, _ in _PRUNE_FILTERS) | {"archived", "include_pinned", "lineage_tips_only"}


def _prune_filter_where(*, archived: Optional[bool] = None, include_pinned: bool = False,
                        lineage_tips_only: bool = False, include_open: bool = False,
                        **filters) -> tuple[str, list]:
    """Shared WHERE clause for bulk prune/archive selection (alias ``s``): ``_PRUNE_FILTERS``
    AND together, only ended sessions are ever candidates, ``archived`` is tri-state
    (None = both), ``*_like`` are case-insensitive substrings, the rest exact.
    ``lineage_tips_only`` (bulk archive) drops compression ancestors: they are archived with
    their tip, never on their own age, and the canonical Bot Chat (the idle sweep's exemption).
    ``include_open`` lifts the ended guard for reversible archive only, never for prune/delete."""
    unknown = set(filters) - _PRUNE_FILTER_NAMES
    if unknown:
        raise TypeError("SessionMaintenanceMixin._prune_filter_where() got an unexpected "
                        f"keyword argument {min(unknown)!r}")
    clauses = [] if include_open else ["s.ended_at IS NOT NULL"]
    if lineage_tips_only:
        clauses.append("COALESCE(s.end_reason, '') <> 'compression'")
        # Bulk archive is housekeeping, like the idle sweep: it never retires the canonical Bot Chat (archiving
        # releases its name). Only a per-chat Archive may. Matched by title alone: compression moves the title
        # onto a visible continuation tip.
        clauses.append("COALESCE(s.title, '') <> 'Bot Chat'")
    params: list = []
    for name, applies, build in _PRUNE_FILTERS:
        value = filters.get(name)
        if (value is not None) if applies == "notnone" else bool(value):
            new_clauses, new_params = build(value)
            clauses.extend(new_clauses)
            params.extend(new_params)
    if isinstance(archived, bool):
        clauses.append(f"s.archived = {int(archived)}")
    # Pinned is a durable "keep" flag: bulk prune/delete/archive exclude pinned rows unless opted in.
    if not include_pinned:
        clauses.append(_not_pinned_sql())
    return " AND ".join(clauses), params


def _prune_where(older_than_days, source, filters, *, whole_lineages: bool = False) -> tuple[str, list]:
    """Translate the legacy age window into the shared activity filter, then build WHERE.
    ``whole_lineages`` (prune) keeps a compression ancestor while any continuation after it
    is unmatched."""
    if (older_than_days is not None and filters.get("last_active_before") is None
            and filters.get("started_before") is None):
        if older_than_days < 0:
            raise ValueError(
                f"older_than_days must be >= 0, got {older_than_days!r}: a negative "
                "retention builds a future cutoff that matches every ended session.")
        filters["last_active_before"] = time.time() - (older_than_days * 86400)
    where, params = _prune_filter_where(source=source, **filters)
    if not whole_lineages:
        return where, params
    # A compressed-away segment ages with its conversation, not on its own: while any later
    # segment stays, deleting it would cut the start off a chat that is still in use.
    return f"{where} AND s.id NOT IN ({_continued_ancestors_sql(where)})", [*params, *params]


def _row_bytes_sql(holder) -> str:
    """Stored size of one ``messages`` row: the byte length of every text/blob column (read from
    the live schema, so a column added later is counted without touching this)."""
    cols = [r[1] for r in holder._read_all("PRAGMA table_info(messages)", [])
            if str(r[2]).upper() in {"TEXT", "BLOB", ""}]
    return " + ".join(f'COALESCE(LENGTH(CAST(m."{c}" AS BLOB)), 0)' for c in cols) or "0"


# The worktree reads wait at most this long on a busy store (less than the store's default read wait).
_WORKTREE_READ_BUSY_S = 2.0


def _inside_tree(worktree_path: str):
    """``inside(path) -> 0/1``: *path* is the tree or under it (native path identity: Windows is
    case-insensitive, either separator; the tree's own spelling and its resolved one)."""
    def key(value) -> str:
        return os.path.normcase(str(value)).replace("\\", "/").rstrip("/")

    roots = {key(Path(worktree_path)), key(Path(worktree_path).resolve())}

    def inside(cwd) -> int:
        return int(bool(cwd) and any(key(cwd) == r or key(cwd).startswith(r + "/") for r in roots))
    return inside


# Whether the tree still serves a session: the gateway's own records that it will still act in a session after its
# last viewer quits (an active /loop, /heartbeat or /goal row, status ``active`` when the field is absent) and a row
# no viewer close ended.
_SERVED_TREE_SQL = (
    "SELECT 1 FROM sessions s WHERE hermes_inside_tree(s.cwd) AND (s.ended_at IS NULL OR EXISTS ("
    "SELECT 1 FROM state_meta m WHERE m.key IN ('loop:'||s.id, 'heartbeat:'||s.id, 'goal:'||s.id) "
    "AND (json_valid(m.value)=0 OR COALESCE(json_extract(m.value,'$.status'),'active')='active'))) LIMIT 1")


def _delegate_sql(available: set[str], alias: str) -> str:
    """Whether ``alias``'s row is a delegate run (``source`` subagent, or a child carrying ``_delegate_from``); ``0``
    when the store has neither column. A missing row (a LEFT JOIN miss) is not one."""
    terms = ([f"coalesce({alias}.source, '') = 'subagent'"] if "source" in available else []) + (
        [f"({alias}.parent_session_id IS NOT NULL AND "
         f"{_sql_json_extract(f'{alias}.model_config', '$._delegate_from')} IS NOT NULL)"]
        if {"parent_session_id", "model_config"} <= available else [])
    return f"({' OR '.join(terms)})" if terms else "0"


def _change_partition_sql(available: set[str], include_subagents: bool) -> tuple[str, str, str, str]:
    """``(with, listed, watched, running)`` for ``session_change_digests``, over ``sessions s``.

    ``listed``: the human session lists' own cut (no internal source; no delegate run unless ``include_subagents``).
    ``watched``: a hidden delegate run whose chain starts at a listed row (a Desktop chat's subagent): no list shows
    it, but a window can watch its transcript. A delegate chain started by a worker (a one-shot, kanban or tool run)
    is internal like its root. ``with`` is the recursive chain walk they need (empty when there is none);
    ``running`` binds ``:running`` (a JSON array of ids)."""
    internal = ", ".join(_sql_literal(source) for source in INTERNAL_LISTING_SOURCES)
    internal_source = f"coalesce(s.source, '') IN ({internal})" if "source" in available else "0"
    delegate = _delegate_sql(available, "s")
    listed = f"NOT ({internal_source})" + ("" if include_subagents or delegate == "0" else f" AND NOT {delegate}")
    running = "s.id IN (SELECT value FROM json_each(:running))" if "id" in available else "0"
    if include_subagents or delegate == "0" or not {"id", "parent_session_id"} <= available:
        return "", listed, "0", running
    # Each delegate run's chain root: the first row up its parent links that is not a delegate run. UNION (not
    # UNION ALL) ends a corrupt cycle; a chain with no such root is watched (the safe side).
    with_sql = (
        "WITH RECURSIVE _delegate_root(id, root_source) AS ("
        f"SELECT s.id, coalesce(p.source, '') FROM sessions s LEFT JOIN sessions p ON p.id = s.parent_session_id "
        f"WHERE {delegate} AND NOT {_delegate_sql(available, 'p')} "
        "UNION SELECT s.id, r.root_source FROM sessions s JOIN _delegate_root r ON s.parent_session_id = r.id "
        f"WHERE {delegate}) ")
    watched = (f"NOT ({internal_source}) AND {delegate} AND s.id NOT IN "
               f"(SELECT id FROM _delegate_root WHERE id IS NOT NULL AND root_source IN ({internal}))")
    return with_sql, listed, watched, running


def _change_digest_columns(fields: tuple[str, ...], declared: Optional[dict[str, str]] = None,
                           ) -> tuple[list[str], list[str], list[str]]:
    """``(listed, counts, internal)`` cell expressions; NULL renders as char(0), apart from ''. REAL-declared
    columns render a real with 17 significant digits, as ``session_projection_digest`` (before SQLite 3.52 the
    text conversion keeps 15: a timestamp moved by microseconds would not move the digest)."""
    declared = declared or {}

    def cell(field: str) -> str:
        kind = declared.get(field, "")
        if "REAL" in kind or "FLOA" in kind or "DOUB" in kind:
            return (f"CASE typeof(s.{field}) WHEN 'real' THEN printf('%!.17g', s.{field}) WHEN 'null' THEN char(0) "
                    f"ELSE s.{field} END")
        return f"coalesce(s.{field}, char(0))"

    listed = [cell(field) for field in fields if field not in SESSION_COUNTER_FIELDS]
    if "message_count" in fields:
        listed.append("(coalesce(s.message_count, 0) > 0)")
    counts = [cell(field) for field in ("id", *SESSION_COUNTER_FIELDS) if field in fields]
    return listed, counts, [cell(field) for field in fields]


def _change_digest_plan(available: set[str], fields: tuple[str, ...], include_subagents: bool,
                        declared: Optional[dict[str, str]] = None):
    """``(with, digests)``: per ``SessionChangeDigests`` digest, its ``(where, cells)`` branches in order."""
    with_sql, listed, watched, running = _change_partition_sql(available, include_subagents)
    listed_cells, count_cells, all_cells = _change_digest_columns(fields, declared)
    return with_sql, (
        ((listed, listed_cells),),
        # ``running`` first where it can cut a partition short: it holds for a handful of rows at most.
        ((f"({listed}) AND NOT ({running})", count_cells), (watched, all_cells)),
        ((f"({running}) AND ({listed})", count_cells),),
        ((f"NOT ({listed}) AND NOT ({watched})", all_cells),),
    )


class SQLiteSessionMaintenance(SessionPortability):
    """Maintenance verbs on the bound profile's ``state.db``."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    # ── prune and sweeps ──────────────────────────────────────────────────

    def prune_sessions(self, older_than_days: Optional[float] = 90, source: Optional[str] = None, *,
                       criteria: PruneCriteria = PruneCriteria(), exclude_active_write_guards: bool = False,
                       media_root: str) -> PruneResult:
        filters = asdict(criteria)
        if filters.get("include_open"):
            # Deletion is not reversible, so the ended-session guard is structural here: the
            # archive-only escape hatch must never reach the delete path.
            raise ValueError("prune_sessions() never deletes open sessions: include_open is archive-only")
        where, where_params = _prune_where(older_than_days, source, filters, whole_lineages=True)
        removed_ids: list[str] = []
        def _do(conn):
            cursor = conn.execute(f"SELECT s.id FROM sessions s WHERE {where}", where_params)
            session_ids = {row["id"] for row in cursor.fetchall()}
            if exclude_active_write_guards:
                session_ids -= _guarded_ids(holder, conn, session_ids)
            session_ids = retire_prunable(conn, sorted(session_ids), media_root=media_root)
            prune_retired_tombstones(conn)  # every prune pass also reclaims expired deletion tombstones
            if not session_ids:
                return 0
            # Batched: a cron-heavy store prunes tens of thousands of ids in one call.
            for chunk in _id_chunks(session_ids):
                ph = _placeholders(chunk)
                conn.execute(f"UPDATE sessions SET parent_session_id = NULL WHERE parent_session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", chunk)
                conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", chunk)
                removed_ids.extend(chunk)
            _delete_unreferenced_system_prompts(conn)
            return len(session_ids)
        with translated():
            holder = self._store.holder()
            count = holder._execute_write(_do)
        return PruneResult(count=count, deleted_ids=tuple(SessionId(sid) for sid in removed_ids))

    def prune_empty_ghost_sessions(self, *, media_root: str) -> tuple[SessionId, ...]:
        cutoff = time.time() - 86400
        def _do(conn):
            ids = [r[0] for r in conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()]
            ids = retire_prunable(conn, ids, media_root=media_root)
            for chunk in _id_chunks(ids):
                conn.execute(f"DELETE FROM sessions WHERE id IN ({_placeholders(chunk)})", chunk)
            if ids:
                _delete_unreferenced_system_prompts(conn)
            return ids
        with translated():
            removed_ids = self._store.holder()._execute_write(_do) or []
        return tuple(SessionId(sid) for sid in removed_ids)

    def sweep_orphaned_sessions(
        self, *, max_idle_seconds: float, sources: tuple[str, ...] = ("tui", "desktop", "subagent"),
        exclude_ids: tuple[str, ...] = (), exclude_pinned: bool = False,
        heartbeat_staleness_seconds: Optional[float] = None,
        heartbeat_ownership_grace_seconds: Optional[float] = None, respect_gateway_heartbeats: bool = True,
    ) -> tuple[SessionId, ...]:
        srcs = tuple(s for s in sources if s)
        if max_idle_seconds <= 0 or not srcs:
            return ()
        hb_staleness, hb_grace = heartbeat_staleness_seconds, heartbeat_ownership_grace_seconds
        if not (hb_staleness and hb_staleness > 0):
            hb_staleness = max_idle_seconds * 2
        if not (hb_grace is not None and hb_grace >= 0):
            hb_grace = hb_staleness
        cutoff = (now := time.time()) - max_idle_seconds
        pin_scope = f" AND {_not_pinned_sql('sessions')}" if exclude_pinned else ""
        orphan_predicate = f"started_at < ? AND {_sql_session_last_active('sessions')} < ?"
        heartbeat_params: tuple[float, ...] = ()
        if respect_gateway_heartbeats:
            orphan_predicate += (" AND NOT EXISTS (SELECT 1 FROM gateway_heartbeats h WHERE"
                                 " h.last_heartbeat >= ? AND h.started_at <= sessions.started_at + ?)")
            heartbeat_params = (now - hb_staleness, hb_grace)
        scope_sql = f" AND source IN ({_placeholders(srcs)}){pin_scope} AND {orphan_predicate}"
        scope_params = (*srcs, cutoff, cutoff, *heartbeat_params)
        def _do(conn):
            rows = conn.execute(f"SELECT id FROM sessions WHERE ended_at IS NULL{scope_sql}",
                                scope_params).fetchall()
            excluded = {str(x) for x in exclude_ids if x}
            victims = [sid for sid in (str(row["id"]) for row in rows)
                       if sid not in excluded and not _write_guards_reject(holder, conn, sid)]
            if not victims:
                return []
            # Re-apply every predicate under the write lock.
            conn.execute(
                f"UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap'"
                f" WHERE id IN ({_placeholders(victims)}) AND ended_at IS NULL{scope_sql}",
                (time.time(), *victims, *scope_params))
            return victims
        with translated():
            holder = self._store.holder()
            return tuple(SessionId(sid) for sid in holder._execute_write(_do) or [])

    # ── selection reads and the idle archive ──────────────────────────────

    def list_prune_candidates(self, older_than_days: Optional[float] = None, source: Optional[str] = None, *,
                              criteria: PruneCriteria = PruneCriteria(), whole_lineages: bool = False,
                              ) -> tuple[PruneCandidate, ...]:
        where, params = _prune_where(older_than_days, source, asdict(criteria), whole_lineages=whole_lineages)
        with translated():
            rows = self._store.holder()._read_all(
                f"""SELECT s.id, s.source, s.title, s.model, s.started_at,
                           {_LAST_ACTIVE_SQL} AS last_active,
                           s.ended_at, s.message_count, s.archived
                    FROM sessions s WHERE {where}
                    ORDER BY last_active ASC, s.started_at ASC""", params)
        return tuple(PruneCandidate(
            session_id=SessionId(row["id"]), source=row["source"], title=row["title"], model=row["model"],
            started_at=row["started_at"], last_active=row["last_active"], ended_at=row["ended_at"],
            message_count=row["message_count"], archived=row["archived"]) for row in rows)

    def count_prune_matches(self, older_than_days: Optional[float] = None, source: Optional[str] = None, *,
                            criteria: PruneCriteria = PruneCriteria(), pinned_only: bool = False) -> int:
        filters = asdict(criteria)
        if pinned_only:
            filters["include_pinned"] = True
        where, params = _prune_where(older_than_days, source, filters)
        if pinned_only:
            where += " AND COALESCE(s.pinned, 0) = 1"
        with translated():
            return int(self._store.holder()._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {where}", params)[0])

    def count_open_prune_matches(self, older_than_days: Optional[float] = None, source: Optional[str] = None, *,
                                 criteria: PruneCriteria = PruneCriteria()) -> int:
        filters = asdict(criteria)
        if filters.get("include_open"):
            return 0
        where, params = _prune_where(older_than_days, source, filters)
        ended_guard = "s.ended_at IS NOT NULL"
        if not where.startswith(ended_guard):
            raise RuntimeError("prune filter lost its ended-session safety guard")
        open_where = f"s.ended_at IS NULL{where[len(ended_guard):]}"
        with translated():
            return int(self._store.holder()._read_one(f"SELECT COUNT(*) FROM sessions s WHERE {open_where}", params)[0])

    def prune_reclaim_estimate(self, older_than_days: Optional[float], source: Optional[str] = None, *,
                               criteria: PruneCriteria = PruneCriteria(), whole_lineages: bool = True,
                               measure_total_bytes: bool = True) -> PruneReclaimEstimate:
        where, params = _prune_where(older_than_days, source, asdict(criteria), whole_lineages=whole_lineages)
        prunable_bytes, total_bytes = 0, None
        with translated():
            holder = self._store.holder()
            prunable, total = tuple(holder._read_one(
                f"SELECT (SELECT COUNT(*) FROM sessions s WHERE {where}), (SELECT COUNT(*) FROM sessions)",
                params) or (0, 0))
            if prunable:
                # Weight by stored bytes, not message_count: old messages are far smaller than recent
                # ones, and message_count skips the soft-archived rows a prune also deletes.
                row_bytes = _row_bytes_sql(holder)
                prunable_bytes = (holder._read_one(
                    f"SELECT COALESCE(SUM({row_bytes}), 0) FROM messages m WHERE m.session_id IN "
                    f"(SELECT s.id FROM sessions s WHERE {where})", params) or (0,))[0]
                if measure_total_bytes:
                    total_bytes = (holder._read_one(
                        f"SELECT COALESCE(SUM({row_bytes}), 0) FROM messages m", []) or (0,))[0]
        return PruneReclaimEstimate(prunable=int(prunable), total=int(total), prunable_bytes=int(prunable_bytes),
                                    total_bytes=None if total_bytes is None else int(total_bytes))

    def list_idle_archive_candidates(self, idle_days: float, *, exclude_pinned: bool = True) -> tuple[SessionId, ...]:
        if idle_days is None or idle_days < 0:
            return ()
        cutoff = time.time() - float(idle_days) * 86400.0
        pin_clause = f"AND {_not_pinned_sql()}" if exclude_pinned else ""
        with translated():
            rows = self._store.holder()._read_all(
                f"""
                SELECT s.id FROM sessions s
                WHERE s.archived = 0
                  AND COALESCE(s.end_reason, '') <> 'compression'
                  {pin_clause}
                  AND NOT (COALESCE(s.hidden, 0) <> 0 AND COALESCE(s.title, '') = ?)
                  AND {_LAST_ACTIVE_SQL} < ?
                ORDER BY s.started_at ASC
                """, (CANONICAL_BOT_CHAT_TITLE, cutoff))
        return tuple(SessionId(row[0]) for row in rows)

    def auto_archive_lineage(self, session_id: SessionId) -> bool:
        # A row already archived keeps its provenance, so a deliberately archived ancestor is never relabelled
        # as sweep-owned (SQLite evaluates every SET expression against the pre-update row).
        with translated():
            holder = self._store.holder()
            return bool(holder._execute_write(
                lambda conn: _set_lineage_column_in_transaction(
                    holder, conn, "archived", session_id, 1,
                    extra_set_sql=", auto_archived = CASE WHEN archived = 0 THEN 1 ELSE auto_archived END")
            ))

    # ── backend heartbeats ────────────────────────────────────────────────

    def register_backend_heartbeat(self, *, backend_id: str, pid: int, started_at: float,
                                   last_heartbeat: Optional[float] = None, profile: str = "", host: str = "") -> None:
        if not backend_id:
            return
        ts = time.time() if last_heartbeat is None else float(last_heartbeat)
        with translated():
            self._store.holder()._write_sql(
                "INSERT INTO gateway_heartbeats (backend_id, pid, started_at, last_heartbeat, profile, host)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(backend_id) DO UPDATE SET pid = excluded.pid,"
                " started_at = excluded.started_at, last_heartbeat = excluded.last_heartbeat,"
                " profile = excluded.profile, host = excluded.host",
                (str(backend_id), int(pid), float(started_at), ts, str(profile), str(host)))

    def clear_backend_heartbeat(self, backend_id: str) -> bool:
        if not backend_id:
            return False
        with translated():
            return self._store.holder()._write_rowcount(
                "DELETE FROM gateway_heartbeats WHERE backend_id = ?", (str(backend_id),)) > 0

    # ── read-only probes ──────────────────────────────────────────────────

    def assert_export_safe(self, session_id: SessionId, max_messages: int) -> int:
        if max_messages < 0:
            raise ValueError("max_messages must be non-negative")
        if max_messages == 0:
            return 0
        with translated():
            row = self._store.holder()._read_one(
                "SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE session_id = ? LIMIT ?)",
                (session_id, max_messages + 1),
            )
        message_count = int(row[0] if row else 0)
        if message_count > max_messages:
            raise ExportTooLarge(session_id, message_count, max_messages)
        return message_count

    def session_projection_digest(self, *, busy_timeout_s: float = 0.05) -> Optional[tuple[tuple[str, ...], bytes]]:
        with translated(), self._store.holder()._read_ctx_with_timeout(busy_timeout_s) as conn:
            declared = {row[1]: str(row[2] or "").upper() for row in conn.execute("PRAGMA table_info(sessions)")}
            available = set(declared)
            fields = tuple(field for field in SESSION_PROJECTION_FIELDS if field in available)
            if not fields:
                return None
            if sqlite3.sqlite_version_info >= _CONCAT_WS_SQLITE:
                # Every row's projected cells joined inside SQLite (the GIL is released while it works), then
                # hashed by hashlib (which releases it too for a large buffer). The change watcher runs this on
                # every commit to every served store; the Python walk below held the GIL for ~75-140 ms per
                # store and stalled every request meanwhile. NULL renders as char(0), apart from ''. Rows come
                # in table order: the digest is only ever compared with this store's previous one.
                # REAL columns render with 17 significant digits: before SQLite 3.52 the text conversion keeps
                # only 15, and a timestamp moved by microseconds would not move the digest.
                cells = ", ".join(
                    f"CASE typeof({field}) WHEN 'real' THEN printf('%!.17g', {field}) WHEN 'null' THEN char(0) "
                    f"ELSE {field} END"
                    if "REAL" in declared[field] or "FLOA" in declared[field] or "DOUB" in declared[field]
                    else f"coalesce({field}, char(0))"
                    for field in fields)
                rendered = conn.execute(
                    f"SELECT CAST(group_concat(concat_ws(char(31), {cells}), char(30)) AS BLOB) FROM sessions",
                ).fetchone()[0]
                return fields, hashlib.blake2b(rendered or b"", digest_size=16).digest()
            order = " ORDER BY id" if "id" in available else ""
            rows = conn.execute(f"SELECT {', '.join(fields)} FROM sessions{order}")
            digest = hashlib.blake2b(digest_size=16)
            for row in rows:
                digest.update(repr(tuple(row)).encode("utf-8", "backslashreplace"))
                digest.update(b"\0")
            return fields, digest.digest()

    def session_change_digests(
        self, *, include_subagents: bool = False, running_ids: Sequence[str] = (), busy_timeout_s: float = 0.05,
    ) -> Optional[SessionChangeDigests]:
        with translated(), self._store.holder()._read_ctx_with_timeout(busy_timeout_s) as conn:
            declared = {row[1]: str(row[2] or "").upper() for row in conn.execute("PRAGMA table_info(sessions)")}
            available = set(declared)
            fields = tuple(field for field in SESSION_PROJECTION_FIELDS if field in available)
            if not fields:
                return None
            with_sql, plan = _change_digest_plan(available, fields, include_subagents, declared)
            params = {"running": json.dumps(sorted(str(sid) for sid in running_ids))}
            if sqlite3.sqlite_version_info >= _CONCAT_WS_SQLITE:
                # One pass inside SQLite (GIL released), as session_projection_digest: each partition's rows joined
                # into one buffer that hashlib digests (it releases the GIL too for a large one).
                aggregates = ", ".join(
                    "CAST(group_concat(CASE " + " ".join(
                        f"WHEN {where} THEN concat_ws(char(31), {', '.join(cells)})" for where, cells in branches)
                    + " END, char(30)) AS BLOB)" for branches in plan)
                rendered = conn.execute(f"{with_sql}SELECT {aggregates} FROM sessions s", params).fetchone()
                digests = [hashlib.blake2b(blob or b"", digest_size=16).digest() for blob in rendered]
            else:
                # Older library (no concat_ws): each digest's branch flagged in SQL, its cells hashed per row in Python.
                flags = ", ".join(
                    "CASE " + " ".join(f"WHEN {where} THEN {index}" for index, (where, _cells) in enumerate(branches, 1))
                    + " ELSE 0 END" for branches in plan)
                select = ", ".join(cell for branches in plan for _where, cells in branches for cell in cells)
                hashers = [hashlib.blake2b(digest_size=16) for _ in plan]
                for row in conn.execute(f"{with_sql}SELECT {flags}, {select} FROM sessions s", params):
                    offset = len(plan)
                    for index, branches in enumerate(plan):
                        for branch, (_where, cells) in enumerate(branches, 1):
                            if row[index] == branch:
                                hashers[index].update(
                                    repr(tuple(row[offset:offset + len(cells)])).encode("utf-8", "backslashreplace"))
                                hashers[index].update(b"\0")
                            offset += len(cells)
                digests = [hasher.digest() for hasher in hashers]
            return SessionChangeDigests(fields, *digests)

    def retained_tree_is_live(self, worktree_path: str, *, pending_only: bool = False) -> bool:
        inside = _inside_tree(worktree_path)
        with translated(), self._store.holder()._read_ctx_with_timeout(_WORKTREE_READ_BUSY_S) as conn:
            conn.create_function("hermes_inside_tree", 1, inside, deterministic=True)
            try:
                row = conn.execute(
                    "SELECT 1 FROM sessions s WHERE hermes_inside_tree(s.cwd) AND ("
                    + ("" if pending_only else "s.message_count>0 OR ")
                    + "EXISTS (SELECT 1 FROM session_admissions a WHERE a.target_session_id=s.id "
                    "AND a.status!='terminal')) LIMIT 1").fetchone()
            finally:
                conn.create_function("hermes_inside_tree", 1, None)
        return row is not None

    def tree_served_sessions(self, worktree_path: str) -> TreeServeFacts:
        with translated(), self._store.holder()._read_ctx_with_timeout(_WORKTREE_READ_BUSY_S) as conn:
            conn.create_function("hermes_inside_tree", 1, _inside_tree(worktree_path), deterministic=True)
            try:
                if conn.execute(_SERVED_TREE_SQL).fetchone():
                    return TreeServeFacts(served=True)
                chats = {row[0] for row in conn.execute(
                    "SELECT chat_id FROM sessions WHERE hermes_inside_tree(cwd) AND chat_id IS NOT NULL")}
            finally:
                conn.create_function("hermes_inside_tree", 1, None)
        return TreeServeFacts(served=False, chat_ids=frozenset(chats))


register_repository("maintenance", SQLiteSessionMaintenance)
