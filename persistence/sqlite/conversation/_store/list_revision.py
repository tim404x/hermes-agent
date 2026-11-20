"""A store-kept revision of what a human-facing session listing can show (fork carry, desktop speed pass).

The Desktop re-lists every profile store on each ``sessions.changed`` and on its own polls, and the listing memo
(``listing_memo``) can only answer from memory while it can prove the store has not moved. The store files' stat
signature moves on EVERY commit, and the default profile commits every ~0.7 s, almost all of it from rows no
human-facing listing ever shows: one-shot/kanban/tool worker runs and sub-agent runs heartbeating, counting tool
calls and appending messages. So that memo never hit on the busiest store.

This revision moves only when something such a listing can show (or depend on) changed. Triggers bump the
``state_meta`` counter ``sessions_visible_list_revision`` in the writer's own transaction, whoever writes (old
builds included, once a current build created them):

- ``sessions``: a row inserted or deleted unless it is INTERNAL; a row updated unless its shape is internal before
  and after (``_internal_core``) and no column a shown lineage reads (id, ``end_reason``, ``chat_id``) moved on a
  row that owns one;
- ``messages``: a row inserted, deleted or updated (any column) unless its session row's shape is internal (a
  missing session row counts as shown): a lineage reads its segments' ids and ``end_reason``, never their messages;
- ``state_meta``: a local-policy receipt whose lineage has more than one segment (``local_lineage_index`` reads
  only those) and names a missing or non-internal-source segment (``_lineage_shown``), before or after the write.

A sessions row is INTERNAL when no listing that excludes ``INTERNAL_SOURCES`` and sub-agent runs can
admit it or be shaped by it:

- it carries ``_delegate_from`` (a sub-agent run: never admitted nor walked as a compression edge by a listing
  that excludes children and sub-agents), or its source is one of ``INTERNAL_SOURCES`` and so is every
  ancestor up to the root (within ``_ANCESTOR_HOPS`` parent hops; a deeper chain counts as shown), so it can
  neither be listed nor be a compression tip of a listed root;
- AND no child of it is a non-internal-source, non-sub-agent row (a branch/reset child's admission reads its
  parent's ``end_reason``/``ended_at``);
- AND neither its id nor its ``chat_id`` (the owner a local lineage segment carries, as
  ``local.local_lineage_owner`` reads it) owns a multi-segment local-policy receipt that names a missing or
  non-internal-source segment (a segment's ``end_reason`` decides which segment of a lineage is listed; a lineage
  of worker rows only, a compressed one-shot run, names nothing a visible listing shows).

Anything else bumps: over-bumping costs a memo miss, under-bumping would serve a stale list. A listing may key
on this revision only when it cannot return internal rows itself (``visible_listing_query``).

Readers verify the whole trigger set on every read (a table rebuild drops a table's triggers; until a writable
open re-creates them the revision is None and callers fall back to the stat signature) and pair the counter with
the schema cookie, which a ``backup()`` restore or any DDL moves even when the restored counter ties.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

REVISION_KEY = "sessions_visible_list_revision"
# persistence.conversation.sessions.INTERNAL_LISTING_SOURCES (the listing deny-list) plus ``cron``: every human
# listing on the Desktop (sidebar recents, messaging slice, roster turns) leaves cron runs out as well, and they write
# every minute on a busy profile. A listing that can show any of these keys on the stat signature instead. Kept
# literal (this module is imported by the DDL module, below the conversation layer); a test pins it to the deny-list.
INTERNAL_SOURCES = ("kanban", "tool", "oneshot", "cron")
POLICY_PREFIX = "gateway.local_policy.v1:"
# Deepest internal chain on the live stores today is 2 hops; deeper chains just count as shown.
_ANCESTOR_HOPS = 6

# The trigger set's version is in every trigger name: readers verify their own names, so a build whose rule differs
# (another internal source set, another hop count) never vouches with this build's triggers, and two builds never
# DROP/CREATE each other's triggers on every writable open. Change any trigger body: bump it
# (test_visible_list_revision pins the SQL's digest to the version). Triggers of another version only add bumps,
# except across a rebuild of a table they name: ``drop_visible_list_triggers`` (below) drops every version first.
TRIGGER_VERSION = 1
TRIGGER_NAMES = tuple(f"{table}_visible_list_rev_v{TRIGGER_VERSION}_{event}"
                      for table in ("sessions", "messages", "state_meta") for event in ("insert", "delete", "update"))
# Every version's names (LIKE pattern, ``\`` escapes the underscores).
_ANY_VERSION_LIKE = r"%\_visible\_list\_rev\_v%"

_INTERNAL_LIST = ", ".join(f"'{source}'" for source in INTERNAL_SOURCES)


def _delegate_marker(alias: str) -> str:
    """The listing's ``_delegate_from`` lookup (``sessions._delegate_from_json``): non-throwing on bad JSON. The
    ``instr`` pre-check skips the JSON parse of every ``model_config`` without the key (nearly all rows): this runs
    per written row, and a bulk prune of a visible session writes thousands."""
    col = f"{alias}.model_config"
    return (f"(CASE WHEN instr({col}, '_delegate_from') THEN json_extract((CASE WHEN json_valid({col}) THEN {col}"
            f" ELSE json_object() END), '$._delegate_from') END)")


def _receipt_lineage_len(alias: str) -> str:
    """``local.LOCAL_LINEAGE_LEN_EXPR`` over a ``state_meta`` alias (or ``new``/``old``)."""
    return (f"(CASE WHEN {alias}.key GLOB '{POLICY_PREFIX}*' AND json_valid({alias}.value) "
            f"THEN json_array_length({alias}.value, '$.lineage') END)")


def _internal_source(expr: str) -> str:
    return f"COALESCE({expr}, '') IN ({_INTERNAL_LIST})"


def _chain_internal(parent_expr: str, hops: int) -> str:
    """1 when the chain above a row (its parent id is *parent_expr*) ends within *hops* parent hops and every
    ancestor on it has an internal source; 0 otherwise (a deeper chain or a dangling parent id counts as shown).

    FLAT on purpose: one correlated scalar subquery over a LEFT JOIN chain (one join per hop) and one flat CASE,
    so the SQL's nesting depth does not grow with *hops*. A walk nested one subquery per hop overflowed the parser
    stack of SQLite < 3.46 (a fixed 100-entry LALR stack; this VPS's system ``sqlite3`` CLI and ``/usr/bin/python3``
    are 3.45.1, macOS ships older still) from 4 hops on, and SQLite parses the whole schema on first use: such a
    store becomes unreadable to every one of those builds (scripts, skills, the recovery salvage CLI).
    ``test_visible_list_revision`` bounds the nesting and parses the DDL on an old SQLite when one is present."""
    joins, whens, prev, prev_alias = [], [], parent_expr, None
    for hop in range(1, hops + 1):
        a = f"_lra{hop}"
        # ``id`` is the PRIMARY KEY: each LEFT JOIN matches at most one row, so the subquery yields exactly one. A hop
        # past a non-internal ancestor is never read (the CASE already answered 0): its join is gated off, so the
        # walk stops early like the nested one did.
        gate = "" if prev_alias is None else f" AND {_internal_source(f'{prev_alias}.source')}"
        joins.append(f" LEFT JOIN sessions {a} ON {a}.id = {prev}{gate}")
        whens.append(f"WHEN {a}.id IS NULL OR NOT {_internal_source(f'{a}.source')} THEN 0"
                     f" WHEN {a}.parent_session_id IS NULL THEN 1")
        prev, prev_alias = f"{a}.parent_session_id", a
    if not joins:
        return f"(CASE WHEN {parent_expr} IS NULL THEN 1 ELSE 0 END)"
    # A root row (most rows) answers without running the joins.
    return (f"(CASE WHEN {parent_expr} IS NULL THEN 1 ELSE (SELECT CASE {' '.join(whens)} ELSE 0 END"
            f" FROM (SELECT 1) _lra0{''.join(joins)}) END)")


def _ancestors_internal(alias: str) -> str:
    """Every ancestor of *alias* within ``_ANCESTOR_HOPS`` hops has an internal source and the chain ends there."""
    return f"{_chain_internal(f'{alias}.parent_session_id', _ANCESTOR_HOPS)} = 1"


def _lineage_shown(alias: str) -> str:
    """SQL truth value: the ``state_meta`` receipt *alias* holds a lineage of more than one segment that a visible
    listing can be shaped by: a segment row is missing or has a non-internal source. A lineage whose segments are
    all worker rows (a compressed ``--source oneshot`` run) only ever names rows such a listing leaves out by
    source, so neither its ``superseded`` ids nor its ``heads`` can touch one of its rows. Non-throwing on bad
    JSON (a trigger condition that raised would fail the write)."""
    value = f"(CASE WHEN json_valid({alias}.value) THEN {alias}.value END)"
    return (f"({_receipt_lineage_len(alias)} > 1 AND EXISTS (SELECT 1 FROM json_each({value}, '$.lineage') _lrs"
            f" WHERE NOT COALESCE((SELECT {_internal_source('_lrg.source')} FROM sessions _lrg"
            f" WHERE _lrg.id = _lrs.value), 0)))")


def _owns_shown_lineage(alias: str) -> str:
    return (f"EXISTS (SELECT 1 FROM state_meta _lrm WHERE _lrm.key IN ('{POLICY_PREFIX}' || {alias}.id,"
            f" '{POLICY_PREFIX}' || {alias}.chat_id) AND {_lineage_shown('_lrm')})")


def _internal_core(alias: str) -> str:
    """The row-shape part of ``internal_row_sql``: a sub-agent run, or a worker run whose chain is all workers, with
    no child a visible listing admits. Cheap: no receipt is read."""
    shown_child = (f"EXISTS (SELECT 1 FROM sessions _lrc WHERE _lrc.parent_session_id = {alias}.id"
                   f" AND NOT {_internal_source('_lrc.source')} AND {_delegate_marker('_lrc')} IS NULL)")
    worker = f"({_internal_source(f'{alias}.source')} AND {_ancestors_internal(alias)})"
    return f"(({_delegate_marker(alias)} IS NOT NULL OR {worker}) AND NOT {shown_child})"


def internal_row_sql(alias: str) -> str:
    """SQL truth value: the ``sessions`` row *alias* (``new``, ``old`` or a table alias) is internal (see module doc)."""
    return f"({_internal_core(alias)} AND NOT {_owns_shown_lineage(alias)})"


def _session_internal(session_id_expr: str) -> str:
    """SQL truth value: the session *session_id_expr* names exists and its messages cannot shape a visible listing.
    A shown lineage reads only its segments' ids and ``end_reason`` (``local.local_lineage_index``), never their
    messages, so the receipt part of ``internal_row_sql`` is left out here: it would JSON-parse the owner's whole
    policy (tens to hundreds of KB) on every message a worker run appends."""
    return f"COALESCE((SELECT {_internal_core('_lrq')} FROM sessions _lrq WHERE _lrq.id = {session_id_expr}), 0)"


def _sessions_update_shown() -> str:
    """The sessions UPDATE condition: the row's shape is not internal before or after, or a column a shown lineage
    reads moved (id, ``end_reason``, ``chat_id``) on a row that owns one. A worker heartbeat on a lineage owner then
    never reads its receipt."""
    lineage_cols = "(old.id IS NOT new.id OR old.end_reason IS NOT new.end_reason OR old.chat_id IS NOT new.chat_id)"
    return (f"NOT {_internal_core('old')} OR NOT {_internal_core('new')}"
            f" OR ({lineage_cols} AND ({_owns_shown_lineage('old')} OR {_owns_shown_lineage('new')}))")


_BUMP = (f"    INSERT INTO state_meta(key, value) VALUES ('{REVISION_KEY}', 1)\n"
         "    ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1;\n")


def _trigger(name: str, event: str, table: str, when: str) -> str:
    return (f"DROP TRIGGER IF EXISTS {name};\n"
            f"CREATE TRIGGER IF NOT EXISTS {name}\nAFTER {event} ON {table}\nWHEN {when}\nBEGIN\n{_BUMP}END;\n")


def _build_trigger_sql() -> str:
    n = iter(TRIGGER_NAMES)
    sess_new, sess_old = internal_row_sql("new"), internal_row_sql("old")
    receipt_new, receipt_old = _lineage_shown("new"), _lineage_shown("old")
    return "".join((
        _trigger(next(n), "INSERT", "sessions", f"NOT {sess_new}"),
        _trigger(next(n), "DELETE", "sessions", f"NOT {sess_old}"),
        _trigger(next(n), "UPDATE", "sessions", _sessions_update_shown()),
        _trigger(next(n), "INSERT", "messages", f"NOT {_session_internal('new.session_id')}"),
        _trigger(next(n), "DELETE", "messages", f"NOT {_session_internal('old.session_id')}"),
        _trigger(next(n), "UPDATE", "messages",
                 f"NOT {_session_internal('old.session_id')} OR NOT {_session_internal('new.session_id')}"),
        _trigger(next(n), "INSERT", "state_meta", receipt_new),
        _trigger(next(n), "DELETE", "state_meta", receipt_old),
        _trigger(next(n), "UPDATE", "state_meta", f"{receipt_old} OR {receipt_new}"),
    ))


# Appended to ``_schema_ddl.DEFERRED_INDEX_SQL``: every writable open creates a missing trigger and replaces one
# whose body changed (``_execute_ddl_skipping_settled_triggers`` skips a settled DROP/CREATE pair).
VISIBLE_LIST_REVISION_SQL = _build_trigger_sql()


def drop_visible_list_triggers(cursor: sqlite3.Cursor, table: Optional[str] = None) -> list[str]:
    """Drop every visible-list revision trigger, of ANY version, whose SQL names *table* (every one when *table* is
    None); returns the dropped names. Run it inside the transaction, BEFORE any statement that renames *table*,
    rebuilds it or drops/renames one of its columns (``_schema._rebuild_table`` does). The messages and state_meta
    triggers read ``sessions``: ``ALTER TABLE sessions RENAME TO sessions_legacy`` rewrites them to name
    ``sessions_legacy``, the ``DROP TABLE`` then leaves them dangling, and every later messages/state_meta write of
    every process (old builds included) fails with "no such table"; ``DROP COLUMN`` refuses outright. Dropping them
    is safe: readers see an incomplete set (and a moved schema cookie), so ``visible_list_revision`` returns None and
    listings fall back to the stat signature until the next writable open of a current build re-creates the set."""
    rows = cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE ? ESCAPE '\\'", (_ANY_VERSION_LIKE,),
    ).fetchall()
    # Over-matching (the word inside a string literal) only drops more: safe. Identifiers may be quoted.
    names_table = None if table is None else re.compile(rf"(?<![\w$]){re.escape(table)}(?![\w$])", re.IGNORECASE)
    dropped = []
    for name, sql in rows:
        if names_table is None or sql is None or names_table.search(sql):
            cursor.execute('DROP TRIGGER IF EXISTS "{}"'.format(name.replace('"', '""')))
            dropped.append(name)
    return dropped


_TRIGGER_MARKS = ",".join("?" * len(TRIGGER_NAMES))
_READ_SQL = (
    "SELECT (SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name IN (" + _TRIGGER_MARKS + ")),"
    " COALESCE((SELECT value FROM state_meta WHERE key = ?), '0')"
)


def visible_list_revision(conn: sqlite3.Connection) -> Optional[tuple[int, int]]:
    """``(revision, schema cookie)`` of the store *conn* reads, or None when the store cannot vouch for one (a
    trigger is missing, no ``state_meta``, a read error). Both values come from one read snapshot."""
    own = not conn.in_transaction
    try:
        if own:
            conn.execute("BEGIN")
        try:
            triggers, value = conn.execute(_READ_SQL, (*TRIGGER_NAMES, REVISION_KEY)).fetchone()
            if triggers != len(TRIGGER_NAMES):
                return None
            cookie = conn.execute("PRAGMA main.schema_version").fetchone()[0]
            return int(value), int(cookie)
        finally:
            if own and conn.in_transaction:
                conn.execute("ROLLBACK")
    except (sqlite3.Error, TypeError, ValueError):
        return None
