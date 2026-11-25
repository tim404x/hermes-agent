"""Immutable result receipts, content-addressed manifests, and outbox/adoption helpers for Kanban.

Delivery semantics (canonical durable event path):
- Completion persists ``tasks.status = 'done'``, run closure, an immutable
  ``kanban_result_receipts`` row, the canonical ``task_events`` (``kind='completed'``)
  row, and a ``kanban_result_outbox`` pending delivery intent inside the SAME
  ``write_txn`` (result-before-notify).
- The existing gateway and TUI notifiers continue to read ``task_events`` via
  ``kanban_notify_subs`` (no duplicate notification channel). Delivery is
  **at-least-once with completion-ID deduplication** (per-subscription
  ``kanban_result_deliveries`` + ``last_ping_event_id``/``last_event_id``):
  retries after transient send or wake failures do not resend already-recorded
  pings or already-settled completions, while external messaging transports
  cannot guarantee exactly-once delivery across a crash between remote accept
  and local checkpoint persistence.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli.sqlite_util import add_column_if_missing

_HASH_CHUNK_BYTES = 1024 * 1024  # 1 MiB streaming chunks outside SQLite writer lock
_BOARD_ID_KEY = "board_id"
_REOPEN_EVENT_KINDS = (
    "reopened",
    "review_reopened",
    "changes_requested",
    "descendant_invalidated",
    "status",
)
_TERMINAL_STATUSES = frozenset({"done", "archived"})

RECEIPT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kanban_receipt_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kanban_result_receipts (
    completion_id      TEXT PRIMARY KEY,
    board_id           TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    input_rev          TEXT NOT NULL,
    attempt            INTEGER NOT NULL,
    run_id             INTEGER,
    status             TEXT NOT NULL,
    artifact_uri       TEXT NOT NULL,
    sha256             TEXT NOT NULL,
    manifest_json      TEXT NOT NULL,
    completed_event_id INTEGER,
    created_at         INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kanban_result_receipts_task_rev
    ON kanban_result_receipts(board_id, task_id, input_rev);
CREATE INDEX IF NOT EXISTS idx_kanban_result_receipts_task
    ON kanban_result_receipts(task_id, created_at);

CREATE TRIGGER IF NOT EXISTS trg_kanban_result_receipts_no_update
BEFORE UPDATE ON kanban_result_receipts
BEGIN
    SELECT RAISE(ABORT, 'kanban_result_receipts rows are immutable');
END;

CREATE TABLE IF NOT EXISTS kanban_result_outbox (
    completion_id      TEXT PRIMARY KEY,
    board_id           TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    completed_event_id INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts  INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT,
    created_at         INTEGER NOT NULL,
    delivered_at       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_kanban_result_outbox_status
    ON kanban_result_outbox(status, task_id, created_at);

CREATE TABLE IF NOT EXISTS kanban_result_deliveries (
    completion_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    ping_sent     INTEGER NOT NULL DEFAULT 0,
    lease_until   INTEGER NOT NULL DEFAULT 0,
    delivered     INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (completion_id, platform, chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_kanban_result_deliveries_task
    ON kanban_result_deliveries(task_id, completion_id);
"""


class CorruptResultReceiptError(RuntimeError):
    """Fail-closed integrity error when a persisted receipt or artifact is missing/corrupt."""


@dataclass(frozen=True)
class ArtifactDigest:
    """Verified byte-level identity of one produced artifact file."""

    uri: str
    path: str
    source_path: str
    filename: str
    size: int
    sha256: str
    stat_sig: tuple[int, int, int, int]  # (st_dev, st_ino, st_size, st_mtime_ns)


@dataclass
class PreparedArtifacts:
    """Artifacts staged and hashed outside SQLite's writer lock before commit."""

    metadata: Optional[dict]
    staged_copies: list[Path]
    attachment_dir: Optional[Path]
    artifact_entries: list[ArtifactDigest]


@dataclass(frozen=True)
class ResultReceipt:
    """Immutable durable result receipt persisted atomically with completion."""

    completion_id: str
    board_id: str
    task_id: str
    input_rev: str
    attempt: int
    run_id: Optional[int]
    status: str
    artifact_uri: str
    sha256: str
    manifest_json: str
    completed_event_id: Optional[int]
    created_at: int

    @property
    def task(self) -> str:
        """Alias matching ``{task, input_rev, attempt, status, artifact_uri, sha256}``."""
        return self.task_id

    @property
    def manifest(self) -> dict[str, Any]:
        parsed = json.loads(self.manifest_json)
        if not isinstance(parsed, dict):
            raise CorruptResultReceiptError("receipt manifest is not a JSON object")
        return parsed

    def to_record(self) -> dict[str, Any]:
        return {
            "completion_id": self.completion_id,
            "board_id": self.board_id,
            "task": self.task_id,
            "task_id": self.task_id,
            "input_rev": self.input_rev,
            "attempt": self.attempt,
            "run_id": self.run_id,
            "status": self.status,
            "artifact_uri": self.artifact_uri,
            "sha256": self.sha256,
            "completed_event_id": self.completed_event_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ResultReceipt:
        return cls(
            completion_id=str(row["completion_id"]),
            board_id=str(row["board_id"]),
            task_id=str(row["task_id"]),
            input_rev=str(row["input_rev"]),
            attempt=int(row["attempt"]),
            run_id=_kb._opt_int(row["run_id"]),
            status=str(row["status"]),
            artifact_uri=str(row["artifact_uri"]),
            sha256=str(row["sha256"]),
            manifest_json=str(row["manifest_json"]),
            completed_event_id=_kb._opt_int(row["completed_event_id"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True)
class OutboxEntry:
    """Durable pending/delivered notification intent tied to a completion ID."""

    completion_id: str
    board_id: str
    task_id: str
    completed_event_id: int
    status: str
    delivery_attempts: int
    last_error: Optional[str]
    created_at: int
    delivered_at: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OutboxEntry:
        return cls(
            completion_id=str(row["completion_id"]),
            board_id=str(row["board_id"]),
            task_id=str(row["task_id"]),
            completed_event_id=int(row["completed_event_id"]),
            status=str(row["status"]),
            delivery_attempts=int(row["delivery_attempts"] or 0),
            last_error=row["last_error"],
            created_at=int(row["created_at"]),
            delivered_at=_kb._opt_int(row["delivered_at"]),
        )


# ---------------------------------------------------------------------------
# Board identity & logical input revision
# ---------------------------------------------------------------------------

def ensure_receipt_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create receipt/outbox tables and immutability triggers."""
    conn.executescript(RECEIPT_SCHEMA_SQL)
    if 'lease_until' not in {row[1] for row in conn.execute('PRAGMA table_info(kanban_result_deliveries)')}:
        with _kb.write_txn(conn):
            add_column_if_missing(conn, 'kanban_result_deliveries', 'lease_until', 'lease_until INTEGER NOT NULL DEFAULT 0')
            conn.execute("INSERT OR IGNORE INTO kanban_result_deliveries(completion_id,task_id,platform,chat_id,thread_id,updated_at) "
                         "SELECT o.completion_id,s.task_id,s.platform,s.chat_id,s.thread_id,o.created_at FROM kanban_result_outbox o "
                         "JOIN kanban_notify_subs s ON s.task_id=o.task_id AND s.created_at<=o.created_at")
            conn.execute("UPDATE kanban_result_outbox SET status='pending',delivered_at=NULL WHERE EXISTS "
                         "(SELECT 1 FROM kanban_result_deliveries d WHERE d.completion_id=kanban_result_outbox.completion_id AND d.delivered=0)")
            conn.execute("UPDATE kanban_result_outbox SET status='no_targets' WHERE status='pending' AND NOT EXISTS "
                         "(SELECT 1 FROM kanban_result_deliveries d WHERE d.completion_id=kanban_result_outbox.completion_id)")


def get_board_id(conn: sqlite3.Connection) -> Optional[str]:
    """Read this board DB's persistent fence ID without writing; None if absent."""
    try:
        row = conn.execute(
            "SELECT value FROM kanban_receipt_meta WHERE key = ?",
            (_BOARD_ID_KEY,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: kanban_receipt_meta' in str(exc):
            return None
        raise
    return str(row["value"]) if row and row["value"] else None


def ensure_board_id(conn: sqlite3.Connection) -> str:
    """Return this board DB's unique persistent ID, creating it once if absent."""
    existing = get_board_id(conn)
    if existing:
        return existing
    candidate = f"brd_{secrets.token_hex(16)}"
    if getattr(conn, "in_transaction", False):
        conn.execute(
            "INSERT OR IGNORE INTO kanban_receipt_meta (key, value) VALUES (?, ?)",
            (_BOARD_ID_KEY, candidate),
        )
    else:
        with _kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO kanban_receipt_meta (key, value) VALUES (?, ?)",
                (_BOARD_ID_KEY, candidate),
            )
    resolved = get_board_id(conn)
    if not resolved:
        raise RuntimeError("failed to initialize kanban board_id fence")
    return resolved


def _reopen_event_fence_id(conn: sqlite3.Connection, task_id: str) -> int:
    """Highest event id representing an explicit reopen / requeue transition.

    Crash retries emit ``claimed``/``crashed``/``timed_out``/``reclaimed``/``stale``/
    ``rate_limited``/``spawn_failed``/``promoted`` — never any of these kinds — so
    this fence stays stable across crash retries while advancing on explicit
    reopens (including dashboard drag-back and direct SQL status reopens with
    an unchanged body).
    """
    placeholders = ",".join("?" for _ in _REOPEN_EVENT_KINDS)
    rows = conn.execute(
        f"SELECT id, kind, payload FROM task_events "
        f"WHERE task_id = ? AND kind IN ({placeholders}) ORDER BY id DESC",
        (task_id, *_REOPEN_EVENT_KINDS),
    ).fetchall()
    for row in rows:
        kind = row["kind"]
        if kind != "status":
            return int(row["id"])
        payload = _kb._json_dict(row["payload"])
        target = str(payload.get("status") or "").strip().lower()
        if target and target not in _TERMINAL_STATUSES:
            return int(row["id"])
    return 0


def compute_input_rev(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    board_id: Optional[str] = None,
) -> str:
    """Compute a deterministic SHA-256 revision of a task's logical inputs.

    Stable across crash retries (claims, heartbeats, PIDs, run rows, scratch
    workspace auto-assignment, completion-staged attachments). Invalidated by:
    - actual input edits (title, body, skills, model/provider/effort overrides,
      goal settings, tenant, project, workspace_kind, dir workspace_path,
      completion_contract, parent edges, or non-completion input attachments),
    - explicit reopen (``reopen_epoch`` or explicit reopen/status events, even
      with an unchanged body),
    - different board DB (``board_id`` fence).
    """
    resolved_board_id = board_id or ensure_board_id(conn)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task {task_id}")
    skills_val = _kb._json_or(_kb._row_get(row, "skills"))
    if isinstance(skills_val, list):
        skills_norm: Any = [str(s) for s in skills_val]
    else:
        skills_norm = None
    workspace_kind = _kb._row_get(row, "workspace_kind", "scratch")
    dir_workspace_path = (
        _kb._row_get(row, "workspace_path")
        if workspace_kind == "dir"
        else None
    )
    input_attachments = [
        [int(a["id"]), str(a["filename"]), int(a["size"] or 0), str(a["stored_path"])]
        for a in conn.execute(
            "SELECT id, filename, size, stored_path FROM task_attachments "
            "WHERE task_id = ? AND COALESCE(uploaded_by, '') != 'kanban_complete' ORDER BY id", (task_id,),
        ).fetchall()
    ]
    payload = {
        "schema": "kanban_input_rev_v2",
        "board_id": resolved_board_id,
        "task_id": str(row["id"]),
        "assignee": row["assignee"],
        "branch_name": _kb._row_get(row, 'branch_name'),
        "workflow": [_kb._row_get(row, field) for field in ("workflow_template_id", "current_step_key")],
        "reopen_epoch": int(_kb._row_get(row, "reopen_epoch", 0) or 0),
        "reopen_event_id": _reopen_event_fence_id(conn, task_id),
        "title": _kb._lossy_text(row["title"]),
        "body": _kb._lossy_text(_kb._row_get(row, "body")),
        "skills": skills_norm,
        "model_override": _kb._row_get(row, "model_override") or None,
        "provider_override": _kb._row_get(row, "provider_override") or None,
        "reasoning_effort": _kb._row_get(row, "reasoning_effort") or None,
        "goal_mode": int(bool(_kb._row_get(row, "goal_mode", 0))),
        "goal_max_turns": _kb._opt_int(_kb._row_get(row, "goal_max_turns")),
        "tenant": _kb._row_get(row, "tenant"),
        "project_id": _kb._row_get(row, "project_id"),
        "resource_class": _kb._row_get(row, "resource_class"),
        "write_scopes": _kb._json_or(_kb._row_get(row, "write_scopes")),
        "workspace_kind": workspace_kind,
        "dir_workspace_path": dir_workspace_path,
        "completion_contract": _kb._row_get(row, "completion_contract"),
        "parents": [list(parent) for parent in conn.execute(
            "SELECT p.id, p.title, p.body, p.assignee, p.reopen_epoch, p.result, r.summary, r.metadata "
            "FROM task_links l JOIN tasks p ON p.id=l.parent_id LEFT JOIN task_runs r ON r.id=("
            "SELECT id FROM task_runs WHERE task_id=p.id AND outcome='completed' ORDER BY id DESC LIMIT 1) "
            "WHERE l.child_id=? ORDER BY p.id", (task_id,),
        )],
        "input_attachments": input_attachments,
        "instructions": [list(comment) for comment in conn.execute(
            "SELECT author,body FROM task_comments WHERE task_id=? AND author NOT IN (?, 'dispatcher') ORDER BY id",
            (task_id, row['assignee'] or ''),
        )],
    }
    binding = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_contract_bound' "
                           "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if binding:
        bound = _kb._json_dict(binding[0])
        if bound.get('pr') == payload['completion_contract']:
            payload['completion_contract'] = bound['repository']
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Streaming artifact hashing & staging (outside SQLite writer lock)
# ---------------------------------------------------------------------------

def _stat_signature(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass(frozen=True)
class CompletionAttempt:
    status: str
    run_id: Optional[int]
    input_rev: str

    def matches(self, conn: sqlite3.Connection, task_id: str) -> bool:
        current = conn.execute('SELECT status, current_run_id FROM tasks WHERE id=?', (task_id,)).fetchone()
        return (current is not None and tuple(current) == (self.status, self.run_id)
                and _kb._parents_satisfied(conn, task_id) and compute_input_rev(conn, task_id) == self.input_rev)


def completion_attempt(conn: sqlite3.Connection, task_id: str, *, expected_run_id: Optional[int], force: bool) -> Optional[CompletionAttempt]:
    row = conn.execute('SELECT t.status,t.current_run_id,t.claim_lock,t.worker_pid,t.worker_started_at,r.input_rev '
                       'FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id AND r.task_id=t.id AND r.ended_at IS NULL '
                       'WHERE t.id=?', (task_id,)).fetchone()
    if row is None or row['status'] not in ('ready', 'running', 'blocked', 'review'):
        return None
    run_id = row['current_run_id']
    if expected_run_id is not None and run_id != int(expected_run_id):
        return None
    if expected_run_id is None and not force and _kb._claim_is_live(row):
        raise _kb.LiveClaimError(task_id)
    current_rev = compute_input_rev(conn, task_id)
    if run_id is not None and row['input_rev'] != current_rev:
        return None
    return CompletionAttempt(row['status'], run_id, current_rev)


def validate_declared_outputs(metadata: Optional[dict]) -> None:
    if isinstance(metadata, dict) and 'artifacts' in metadata:
        declared = metadata['artifacts']
        if not isinstance(declared, (tuple, list)) or any(not isinstance(path, str) or not path.strip() for path in declared):
            raise _kb.ArtifactPreservationError('artifacts must be a list of nonempty file paths')


def _open_verified_regular_fd(path: Path, label: str, *, error_cls: type[Exception]) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    except OSError as exc:
        raise error_cls(f"declared artifact is unavailable or unreadable: {label} ({exc})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise error_cls(f"declared artifact is not a regular file: {label}")
        return fd, st
    except Exception:
        os.close(fd)
        raise


def _assert_file_unchanged(
    resolved: Path,
    st_before: os.stat_result,
    fd: int,
    bytes_read: int,
    label: str,
    *,
    error_cls: type[Exception],
) -> tuple[int, int, int, int]:
    st_after = os.fstat(fd)
    try:
        st_path = os.stat(resolved)
    except OSError as exc:
        raise error_cls(f"declared artifact vanished while hashing: {label} ({exc})") from exc
    sig_before = _stat_signature(st_before)
    sig_after = _stat_signature(st_after)
    sig_path = _stat_signature(st_path)
    if sig_before != sig_after or sig_before != sig_path or bytes_read != st_before.st_size:
        raise error_cls(f"declared artifact changed while hashing: {label}")
    return sig_after


def _stream_hash_file(
    resolved: Path,
    label: str,
    *,
    error_cls: type[Exception],
) -> tuple[str, int, tuple[int, int, int, int]]:
    """Stream-hash a regular file in 1 MiB chunks, failing closed if it changes mid-read."""
    fd, st_before = _open_verified_regular_fd(resolved, label, error_cls=error_cls)
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
        sig = _assert_file_unchanged(resolved, st_before, fd, total, label, error_cls=error_cls)
        return digest.hexdigest(), total, sig
    finally:
        os.close(fd)


def _stream_copy_and_hash(
    resolved_src: Path,
    dest: Path,
    label: str,
    *,
    max_bytes: int,
) -> tuple[str, int, tuple[int, int, int, int]]:
    """Copy+hash a scratch artifact in one streaming pass, fsyncing the pinned copy."""
    err_cls = _kb.ArtifactPreservationError
    fd, st_before = _open_verified_regular_fd(resolved_src, label, error_cls=err_cls)
    if st_before.st_size > max_bytes:
        os.close(fd)
        raise err_cls(f"declared artifact exceeds the {max_bytes}-byte limit: {label}")
    digest = hashlib.sha256()
    total = 0
    owned = False
    try:
        with dest.open("xb") as dst:
            owned = True
            while True:
                chunk = os.read(fd, _HASH_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise err_cls(f"declared artifact grew beyond the size limit: {label}")
                dst.write(chunk)
                digest.update(chunk)
            dst.flush()
            os.fsync(dst.fileno())
            dest_sig = _stat_signature(os.fstat(dst.fileno()))
        _assert_file_unchanged(resolved_src, st_before, fd, total, label, error_cls=err_cls)
        return digest.hexdigest(), total, dest_sig
    except BaseException:
        if owned:
            dest.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)


def prepare_completion_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
) -> PreparedArtifacts:
    """Pin every declared output before taking the board writer lock."""
    if conn.in_transaction:
        raise RuntimeError("artifact preparation requires an unlocked connection")
    paths = _kb._cleaned_artifact_paths(metadata)
    if not paths:
        return PreparedArtifacts(metadata, [], None, [])

    from agent.file_safety import get_read_block_error
    from gateway.platforms.base import validate_media_delivery_path
    from hermes_cli.kanban_db_connect import _main_db_file

    db_file = _main_db_file(conn)
    board = None
    if db_file:
        parent = Path(db_file).parent
        if parent.parent == _kb.boards_root():
            board = parent.name
        elif Path(db_file) == _kb.kanban_home() / "kanban.db":
            board = _kb.DEFAULT_BOARD
    directory = _kb.task_attachments_dir(task_id, board=board).resolve()
    task = _kb.get_task(conn, task_id)
    workspace = Path(task.workspace_path) if task and task.workspace_path else Path.cwd()
    directory.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    entries: list[ArtifactDigest] = []
    try:
        for artifact in dict.fromkeys(paths):
            source = workspace / Path(artifact).expanduser()
            if get_read_block_error(str(source)):
                raise _kb.ArtifactPreservationError(f"declared artifact is not readable: {artifact}")
            source = source.resolve(strict=True)
            if not validate_media_delivery_path(str(source)):
                raise _kb.ArtifactPreservationError(f"declared artifact is unavailable or not deliverable: {artifact}")
            dest = _kb._unique_attachment_path(directory, source.name, set(staged)).absolute()
            digest, size, signature = _stream_copy_and_hash(source, dest, artifact, max_bytes=_kb.KANBAN_ATTACHMENT_MAX_BYTES)
            staged.append(dest)
            entries.append(ArtifactDigest(dest.as_uri(), str(dest), str(source), dest.name, size, digest, signature))
        if hasattr(os, 'O_DIRECTORY'):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException as exc:
        _kb._discard_staged_copies(staged, directory)
        if isinstance(exc, (OSError, ValueError)):
            raise _kb.ArtifactPreservationError(f"could not preserve declared artifact: {exc}") from exc
        raise
    return PreparedArtifacts({**metadata, "artifacts": [str(path) for path in staged]}, staged, directory, entries)


def discard_prepared_artifacts(conn: sqlite3.Connection, task_id: str, prepared: PreparedArtifacts) -> None:
    """A post-COMMIT check can raise too: only discard files without a durable owner."""
    if prepared.staged_copies and prepared.attachment_dir is not None:
        registered = {row[0] for row in conn.execute('SELECT stored_path FROM task_attachments WHERE task_id=?', (task_id,))}
        _kb._discard_staged_copies([p for p in prepared.staged_copies if str(p.resolve()) not in registered], prepared.attachment_dir)


def verify_prepared_artifacts_unchanged(prepared: PreparedArtifacts) -> None:
    """O(1) pre-commit stat guard inside ``write_txn`` (zero hashing under lock)."""
    for entry in prepared.artifact_entries:
        try:
            st = os.stat(entry.path)
        except OSError as exc:
            raise _kb.ArtifactPreservationError(
                f"declared artifact disappeared before commit: {entry.path}"
            ) from exc
        if not stat.S_ISREG(st.st_mode) or _stat_signature(st) != entry.stat_sig:
            raise _kb.ArtifactPreservationError(
                f"declared artifact changed between hashing and commit: {entry.path}"
            )


# ---------------------------------------------------------------------------
# Atomic receipt + completed event + pending outbox persistence (inside write_txn)
# ---------------------------------------------------------------------------

def _attempt_number(conn: sqlite3.Connection, task_id: str, run_id: Optional[int]) -> int:
    if run_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND id <= ?",
            (task_id, int(run_id)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return max(1, int(row[0]) if row else 1)


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_and_persist_receipt_in_txn(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    input_rev: str,
    run_id: Optional[int],
    result: Optional[str],
    event_summary: Optional[str],
    handoff_summary: Optional[str],
    metadata: Optional[dict],
    verified_cards: list[str],
    prepared: PreparedArtifacts,
    now: int,
) -> tuple[ResultReceipt, int]:
    """Persist completed event + immutable receipt + pending outbox inside caller's write_txn."""
    verify_prepared_artifacts_unchanged(prepared)
    for staged_path in prepared.staged_copies:
        _kb._insert_completion_attachment(
            conn, task_id, filename=staged_path.name,
            stored_path=str(staged_path.resolve()),
            size=staged_path.stat().st_size, created_at=now,
            uploaded_by="kanban_complete",
        )
    board_id = ensure_board_id(conn)
    attempt = _attempt_number(conn, task_id, run_id)
    completion_id = f"rcpt_{secrets.token_hex(16)}"

    manifest_artifacts = [
        {
            "uri": entry.uri,
            "path": entry.path,
            "source_path": entry.source_path,
            "filename": entry.filename,
            "size": entry.size,
            "sha256": entry.sha256,
        }
        for entry in prepared.artifact_entries
    ]
    artifact_uri = f"receipt://{board_id}/{completion_id}/manifest"

    manifest = {
        "schema": "kanban_result_manifest_v2",
        "artifact_uri": artifact_uri,
        "completion_id": completion_id,
        "board_id": board_id,
        "task_id": task_id,
        "input_rev": input_rev,
        "attempt": attempt,
        "run_id": run_id,
        "status": "done",
        "result": result,
        "summary": handoff_summary,
        "metadata": metadata if isinstance(metadata, dict) else None,
        "verified_cards": list(verified_cards),
        "artifacts": manifest_artifacts,
    }
    manifest_json = _canonical_json(manifest)
    receipt_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

    event_payload = _kb._completed_event_payload(result, event_summary, verified_cards, metadata)
    event_payload.update(
        {
            "completion_id": completion_id,
            "board_id": board_id,
            "input_rev": input_rev,
            "attempt": attempt,
            "artifact_uri": artifact_uri,
            "receipt_sha256": receipt_sha256,
            "delivery_intent": "pending",
        }
    )
    ev_cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, 'completed', ?, ?)",
        (task_id, run_id, _kb._json_or_null(event_payload), now),
    )
    completed_event_id = int(ev_cur.lastrowid or 0)

    conn.execute(
        """
        INSERT INTO kanban_result_receipts (
            completion_id, board_id, task_id, input_rev, attempt, run_id,
            status, artifact_uri, sha256, manifest_json, completed_event_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, ?)
        """,
        (
            completion_id, board_id, task_id, input_rev, attempt, run_id,
            artifact_uri, receipt_sha256, manifest_json, completed_event_id, now,
        ),
    )
    conn.execute(
        """
        INSERT INTO kanban_result_outbox (
            completion_id, board_id, task_id, completed_event_id,
            status, delivery_attempts, last_error, created_at, delivered_at
        ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?, NULL)
        """,
        (completion_id, board_id, task_id, completed_event_id, now),
    )
    targets = conn.execute(
        "INSERT INTO kanban_result_deliveries(completion_id, task_id, platform, chat_id, thread_id, updated_at) "
        "SELECT ?, task_id, platform, chat_id, thread_id, ? FROM kanban_notify_subs WHERE task_id=?",
        (completion_id, now, task_id),
    )
    if not targets.rowcount:
        conn.execute("UPDATE kanban_result_outbox SET status='no_targets' WHERE completion_id=?", (completion_id,))
    receipt = ResultReceipt(
        completion_id=completion_id,
        board_id=board_id,
        task_id=task_id,
        input_rev=input_rev,
        attempt=attempt,
        run_id=run_id,
        status="done",
        artifact_uri=artifact_uri,
        sha256=receipt_sha256,
        manifest_json=manifest_json,
        completed_event_id=completed_event_id,
        created_at=now,
    )
    return receipt, completed_event_id


# ---------------------------------------------------------------------------
# Queries & fail-closed verification
# ---------------------------------------------------------------------------

def get_result_receipt(conn: sqlite3.Connection, completion_id: str) -> Optional[ResultReceipt]:
    row = conn.execute(
        "SELECT * FROM kanban_result_receipts WHERE completion_id = ?",
        (completion_id,),
    ).fetchone()
    return ResultReceipt.from_row(row) if row else None


def list_result_receipts(conn: sqlite3.Connection, task_id: str) -> list[ResultReceipt]:
    rows = conn.execute(
        "SELECT * FROM kanban_result_receipts WHERE task_id = ? ORDER BY created_at ASC, rowid ASC",
        (task_id,),
    ).fetchall()
    return [ResultReceipt.from_row(r) for r in rows]


def get_receipt_for_input_rev(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    input_rev: Optional[str] = None,
    board_id: Optional[str] = None,
) -> Optional[ResultReceipt]:
    resolved_board_id = board_id or get_board_id(conn)
    if not resolved_board_id:
        return None
    resolved_rev = input_rev or compute_input_rev(conn, task_id, board_id=resolved_board_id)
    row = conn.execute(
        "SELECT * FROM kanban_result_receipts "
        "WHERE board_id = ? AND task_id = ? AND input_rev = ? LIMIT 1",
        (resolved_board_id, task_id, resolved_rev),
    ).fetchone()
    return ResultReceipt.from_row(row) if row else None


def get_outbox_entry(conn: sqlite3.Connection, completion_id: str) -> Optional[OutboxEntry]:
    row = conn.execute(
        "SELECT * FROM kanban_result_outbox WHERE completion_id = ?",
        (completion_id,),
    ).fetchone()
    return OutboxEntry.from_row(row) if row else None


def list_pending_outbox(conn: sqlite3.Connection, task_id: Optional[str] = None) -> list[OutboxEntry]:
    if task_id is None:
        rows = conn.execute(
            "SELECT * FROM kanban_result_outbox WHERE status = 'pending' ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kanban_result_outbox WHERE status = 'pending' AND task_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (task_id,),
        ).fetchall()
    return [OutboxEntry.from_row(r) for r in rows]


def verify_result_receipt(
    conn: sqlite3.Connection,
    receipt: ResultReceipt,
    *,
    expected_task_id: Optional[str] = None,
    expected_input_rev: Optional[str] = None,
    check_artifacts: bool = True,
    verified_stats: Optional[dict] = None,
) -> dict[str, Any]:
    """Verify a receipt's board fence, manifest digest, and produced artifacts fail-closed.

    Artifact hashing runs outside SQLite's writer lock. Raises
    :class:`CorruptResultReceiptError` on any mismatch, corruption, or missing file.
    """
    if check_artifacts and getattr(conn, "in_transaction", False):
        raise RuntimeError("verify_result_receipt(check_artifacts=True) must run outside SQLite write_txn")
    board_id = get_board_id(conn)
    if not board_id or receipt.board_id != board_id:
        raise CorruptResultReceiptError(
            f"receipt {receipt.completion_id} board_id mismatch "
            f"(receipt={receipt.board_id!r}, board={board_id!r})"
        )
    if expected_task_id is not None and receipt.task_id != expected_task_id:
        raise CorruptResultReceiptError(
            f"receipt {receipt.completion_id} task_id mismatch "
            f"(receipt={receipt.task_id!r}, expected={expected_task_id!r})"
        )
    if expected_input_rev is not None and receipt.input_rev != expected_input_rev:
        raise CorruptResultReceiptError(
            f"receipt {receipt.completion_id} input_rev mismatch"
        )
    actual_sha = hashlib.sha256(receipt.manifest_json.encode("utf-8")).hexdigest()
    if actual_sha != receipt.sha256:
        raise CorruptResultReceiptError(
            f"receipt {receipt.completion_id} manifest sha256 mismatch "
            f"(expected={receipt.sha256}, actual={actual_sha})"
        )
    try:
        manifest = json.loads(receipt.manifest_json)
    except Exception as exc:
        raise CorruptResultReceiptError(
            f"receipt {receipt.completion_id} manifest JSON malformed: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} manifest is not an object")

    for field_name, expected in (
        ("completion_id", receipt.completion_id),
        ("board_id", receipt.board_id),
        ("task_id", receipt.task_id),
        ("input_rev", receipt.input_rev),
        ("attempt", receipt.attempt),
        ("run_id", receipt.run_id),
        ("status", receipt.status),
    ):
        if manifest.get(field_name) != expected:
            raise CorruptResultReceiptError(
                f"receipt {receipt.completion_id} manifest field {field_name!r} mismatch"
            )

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} manifest artifacts must be a list")
    manifest_uri = f"receipt://{board_id}/{receipt.completion_id}/manifest"
    if manifest.get('schema') == 'kanban_result_manifest_v1':
        expected_uri = artifacts[0].get('uri') if len(artifacts) == 1 and isinstance(artifacts[0], dict) else manifest_uri
    elif manifest.get('schema') == 'kanban_result_manifest_v2' and manifest.get('artifact_uri') == manifest_uri:
        expected_uri = manifest_uri
    else:
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} unsupported manifest or URI")
    if receipt.artifact_uri != expected_uri:
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} manifest URI mismatch")

    if check_artifacts:
        for item in artifacts:
            path, signature = _verify_artifact(receipt, item)
            if verified_stats is not None:
                verified_stats[path] = signature
    return manifest


def _verify_artifact(receipt: ResultReceipt, item: dict) -> tuple[str, tuple]:
    if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not isinstance(item.get('size'), int):
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} malformed artifact entry")
    path = Path(item['path'])
    if not path.is_absolute() or path.as_uri() != item.get('uri'):
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} artifact URI mismatch")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} artifact missing: {path}") from exc
    digest, size, signature = _stream_hash_file(resolved, str(path), error_cls=CorruptResultReceiptError)
    if size != item['size']:
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} artifact size mismatch for {path}")
    if digest != item.get('sha256'):
        raise CorruptResultReceiptError(f"receipt {receipt.completion_id} artifact sha256 mismatch for {path}")
    return str(path), signature


def check_verified_pins(signatures: dict) -> None:
    for path, signature in signatures.items():
        try:
            unchanged = _stat_signature(os.stat(path, follow_symlinks=False)) == signature
        except OSError:
            unchanged = False
        if not unchanged:
            raise CorruptResultReceiptError(f"verified artifact changed: {path}")


# ---------------------------------------------------------------------------
# Adoption at retry/replay boundaries
# ---------------------------------------------------------------------------

def adopt_result_receipt(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_board_id: Optional[str] = None,
    expected_input_rev: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    dry_run: bool = False,
) -> Optional[ResultReceipt]:
    """Adopt an existing verified receipt for ``(board_id, task_id, current_input_rev)``.

    Returns the original immutable :class:`ResultReceipt` when a verified receipt
    matches this board and logical input revision, reconciling ``tasks.status``
    to ``done`` and running idempotent post-commit child promotion WITHOUT
    opening a new run, spawning an executor, or duplicating ``completed`` events
    or outbox rows.

    Returns ``None`` when no receipt matches (never completed, input edited,
    explicitly reopened, different board, stale ``expected_run_id``, or
    unsatisfied parents). Raises :class:`CorruptResultReceiptError` fail-closed
    when a matching receipt exists but fails manifest or artifact verification.
    """
    board_id = get_board_id(conn)
    if not board_id:
        return None
    if expected_board_id is not None and expected_board_id != board_id:
        return None
    task = _kb.get_task(conn, task_id)
    if (task is None or task.status not in ('done', 'ready') or task.current_run_id is not None
            or task.claim_lock or not conn.execute(
                "SELECT 1 FROM kanban_result_receipts WHERE task_id=? LIMIT 1", (task_id,),
            ).fetchone()):
        return None
    current_rev = compute_input_rev(conn, task_id, board_id=board_id)
    if expected_input_rev is not None and expected_input_rev != current_rev:
        return None
    receipt = get_receipt_for_input_rev(conn, task_id, input_rev=current_rev, board_id=board_id)
    if receipt is None:
        return None
    if expected_run_id is not None and receipt.run_id != int(expected_run_id):
        return None
    if not _kb._parents_satisfied(conn, task_id):
        return None

    # Fail closed outside SQLite's writer lock before mutating any state.
    verified_stats = {}
    manifest = verify_result_receipt(
        conn, receipt, expected_task_id=task_id, expected_input_rev=current_rev, check_artifacts=True,
        verified_stats=verified_stats,
    )
    with _kb.write_txn(conn):
        if not _kb._parents_satisfied(conn, task_id):
            return None
        # Re-check revision under lock in case an edit/reopen raced verification.
        current = _kb.get_task(conn, task_id)
        if (current is None or current.status != task.status or current.claim_lock or current.current_run_id is not None
                or get_result_receipt(conn, receipt.completion_id) != receipt or get_board_id(conn) != board_id):
            return None
        if compute_input_rev(conn, task_id, board_id=board_id) != current_rev:
            return None
        check_verified_pins(verified_stats)
        if dry_run:
            return receipt
        if current.status != "done":
            # Reconcile back to done without opening a new run or emitting a second
            # completed event/outbox row (original receipt + outbox remain canonical).
            conn.execute(
                """
                UPDATE tasks
                   SET status            = 'done',
                       result            = ?,
                       completed_at      = COALESCE(completed_at, ?),
                       claim_lock        = NULL,
                       claim_expires     = NULL,
                       worker_pid        = NULL,
                       worker_started_at = NULL,
                       current_run_id    = NULL,
                       block_kind        = NULL,
                       block_recurrences = 0, consecutive_failures = 0, last_failure_error = NULL
                 WHERE id = ?
                   AND status = 'ready' AND current_run_id IS NULL AND claim_lock IS NULL
                """,
                (manifest.get("result"), receipt.created_at, task_id),
            )
    # Idempotent post-commit housekeeping in case the original attempt crashed
    # right after commit before clearing failures or promoting children.
    _kb.recompute_ready(conn)
    return receipt


def try_adopt_before_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    dry_run: bool = False,
) -> Optional[ResultReceipt]:
    """Check for a verified result before mutating a claim."""
    return adopt_result_receipt(conn, task_id, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Explicit reopen (new logical revision even with unchanged body)
# ---------------------------------------------------------------------------

def reopen_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    target_status: str = "ready",
    reason: Optional[str] = None,
    author: str = "operator",
) -> bool:
    """Explicitly reopen a ``done`` / ``archived`` / ``review`` task as a new logical revision.

    Increments ``reopen_epoch`` (invalidating ``input_rev`` even when ``body`` is
    unchanged), appends a ``reopened`` event, and atomically invalidates
    descendants when reopening a ``done`` or ``archived`` parent.
    """
    if target_status not in ("ready", "todo", "triage"):
        raise ValueError(f"target_status must be 'ready', 'todo', or 'triage', got {target_status!r}")
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, reopen_epoch FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False
        prior_status = str(row["status"])
        if prior_status not in ("done", "archived", "review"):
            return False
        if target_status == "ready":
            effective_status = _kb._landing_status_after_parents(conn, task_id)
        else:
            effective_status = target_status
        new_epoch = int(_kb._row_get(row, "reopen_epoch", 0) or 0) + 1
        cur = conn.execute(
            """
            UPDATE tasks
               SET status               = ?,
                   completed_at         = NULL,
                   claim_lock           = NULL,
                   claim_expires        = NULL,
                   worker_pid           = NULL,
                   worker_started_at    = NULL,
                   current_run_id       = NULL,
                   consecutive_failures = 0,
                   last_failure_error   = NULL,
                   reopen_epoch         = ?
             WHERE id = ? AND status = ?
            """,
            (effective_status, new_epoch, task_id, prior_status),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn,
            task_id,
            "reopened",
            {
                "prior_status": prior_status,
                "status": effective_status,
                "reopen_epoch": new_epoch,
                "reason": reason,
                "actor": author,
            },
        )
        if prior_status in ("done", "archived"):
            inv = _kb.invalidate_descendants_for_parent_reopen(conn, task_id, author=author)
            terminations.extend(inv["terminations"])
    for pid, claim_lock, started_at in terminations:
        _kb._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return True


# ---------------------------------------------------------------------------
# Outbox delivery tracking & dedup helpers (for canonical notifier path)
# ---------------------------------------------------------------------------

def _delivery_key(
    completion_id: str, platform: str, chat_id: str, thread_id: Optional[str],
) -> tuple[str, str, str, str]:
    return (completion_id, platform, chat_id, thread_id or "")


def is_outbox_ping_sent(
    conn: sqlite3.Connection,
    completion_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    row = conn.execute(
        "SELECT ping_sent, delivered FROM kanban_result_deliveries "
        "WHERE completion_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        _delivery_key(completion_id, platform, chat_id, thread_id),
    ).fetchone()
    return bool(row and (row["ping_sent"] or row["delivered"]))


def is_outbox_delivered(
    conn: sqlite3.Connection,
    completion_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    row = conn.execute(
        "SELECT delivered FROM kanban_result_deliveries "
        "WHERE completion_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        _delivery_key(completion_id, platform, chat_id, thread_id),
    ).fetchone()
    return bool(row and row["delivered"])


def record_outbox_ping_sent(
    conn: sqlite3.Connection,
    completion_id: str,
    task_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> None:
    key = _delivery_key(completion_id, platform, chat_id, thread_id)
    with _kb.write_txn(conn):
        conn.execute("UPDATE kanban_result_deliveries SET ping_sent=1, updated_at=? WHERE completion_id=? "
                     "AND platform=? AND chat_id=? AND thread_id=? AND task_id=? AND delivered=0",
                     (int(time.time()), *key, task_id))


def record_outbox_delivery_failure(
    conn: sqlite3.Connection,
    completion_ids: Iterable[str],
    task_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    error: str = "",
) -> None:
    ids = [str(cid) for cid in dict.fromkeys(completion_ids) if cid]
    if not ids:
        return
    now = int(time.time())
    err_excerpt = str(error)[:400] if error else None
    with _kb.write_txn(conn):
        for cid in ids:
            key = _delivery_key(cid, platform, chat_id, thread_id)
            changed = conn.execute(
                "UPDATE kanban_result_deliveries SET attempts=attempts+1, lease_until=0, last_error=?, updated_at=? "
                "WHERE completion_id=? AND platform=? AND chat_id=? AND thread_id=? AND task_id=? AND delivered=0",
                (err_excerpt, now, *key, task_id),
            )
            if not changed.rowcount:
                continue
            conn.execute(
                """
                UPDATE kanban_result_outbox
                   SET delivery_attempts = delivery_attempts + 1,
                       last_error = ?
                 WHERE completion_id = ? AND status = 'pending'
                """,
                (err_excerpt, cid),
            )


def mark_outbox_delivered(
    conn: sqlite3.Connection,
    completion_ids: Iterable[str],
    task_id: str,
    *,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    verified_stats: Optional[dict] = None,
) -> None:
    """ACK only this destination. A subscription cursor is never evidence of delivery."""
    ids = set(completion_ids)
    if verified_stats is None:
        verified_stats = {}
        for cid in ids:
            receipt = get_result_receipt(conn, cid)
            if receipt is None:
                raise CorruptResultReceiptError(f'missing result receipt: {cid}')
            verify_result_receipt(conn, receipt, expected_task_id=task_id, verified_stats=verified_stats)
    with _kb.write_txn(conn):
        check_verified_pins(verified_stats)
        for cid in ids:
            key = _delivery_key(cid, platform, chat_id, thread_id)
            now = int(time.time())
            changed = conn.execute(
                "UPDATE kanban_result_deliveries SET delivered=1, lease_until=0, last_error=NULL, updated_at=? "
                "WHERE completion_id=? AND platform=? AND chat_id=? AND thread_id=? AND task_id=? AND delivered=0",
                (now, *key, task_id),
            )
            if changed.rowcount:
                conn.execute(
                    "UPDATE kanban_result_outbox SET status='delivered', delivered_at=?, last_error=NULL "
                    "WHERE completion_id=? AND NOT EXISTS (SELECT 1 FROM kanban_result_deliveries "
                    "WHERE completion_id=? AND delivered=0)", (now, cid, cid),
                )


def verified_delivery_paths(conn: sqlite3.Connection, task_id: str, event) -> dict:
    payload = event.payload
    receipt = get_result_receipt(conn, payload['completion_id'])
    if (receipt is None or receipt.completed_event_id != event.id or receipt.artifact_uri != payload.get('artifact_uri')
            or receipt.sha256 != payload.get('receipt_sha256')):
        raise CorruptResultReceiptError("completion event does not match its receipt")
    signatures = {}
    manifest = verify_result_receipt(conn, receipt, expected_task_id=task_id, verified_stats=signatures)
    paths = [entry['path'] for entry in manifest['artifacts']]
    if paths != payload.get('artifacts', []):
        raise CorruptResultReceiptError("completion event does not match its pinned outputs")
    return signatures


def verify_completion_artifacts_for_delivery(
    conn: sqlite3.Connection,
    completion_id: str,
) -> tuple[bool, Optional[str]]:
    """Verify a completion receipt's artifacts before uploading them in the notifier."""
    receipt = get_result_receipt(conn, completion_id)
    if receipt is None:
        return False, f"missing result receipt for completion_id={completion_id}"
    try:
        verify_result_receipt(conn, receipt, check_artifacts=True)
    except CorruptResultReceiptError as exc:
        return False, str(exc)
    return True, None


def dispatch_pre_admit_receipt(
    conn: sqlite3.Connection,
    task_id: str,
    result: Any,
    *,
    dry_run: bool = False,
) -> bool:
    """Pre-admission receipt check at dispatch replay boundary.

    Returns True if the task was handled (either adopted from a verified receipt
    or refused fail-closed on corruption) and must NOT be claimed/spawned.
    """
    try:
        adopted = try_adopt_before_claim(conn, task_id, dry_run=dry_run)
    except CorruptResultReceiptError as exc:
        result.corrupt_receipts.append(task_id)
        if not dry_run:
            with _kb.write_txn(conn):
                last = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                err_payload = {"error": str(exc)[:400]}
                if (
                    last is None
                    or last["kind"] != "receipt_verification_failed"
                    or last["payload"] != _kb._json_or_null(err_payload)
                ):
                    _kb._append_event(conn, task_id, "receipt_verification_failed", err_payload)
        return True
    if adopted is not None:
        result.adopted.append(task_id)
        return True
    return False


def _copy_review_artifact(source: Path, destination: Path, label: str) -> None:
    fd, _ = _open_verified_regular_fd(source, label, error_cls=_kb.ArtifactPreservationError)
    with os.fdopen(fd, 'rb') as reader, destination.open('xb') as writer:
        try:
            copied = 0
            while chunk := reader.read(_HASH_CHUNK_BYTES):
                copied += len(chunk)
                if copied > _kb.KANBAN_ATTACHMENT_MAX_BYTES:
                    raise _kb.ArtifactPreservationError(f"declared review artifact exceeds the size limit: {label}")
                writer.write(chunk)
        except BaseException:
            writer.close()
            destination.unlink(missing_ok=True)
            raise


def persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return
    workspace = _kb._scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _kb._managed_scratch_path_info(workspace)
    if not is_managed:
        return
    try:
        workspace_root = workspace.resolve()
    except OSError:
        return
    attachment_dir = _kb.task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        _kb._discard_staged_copies(used_destinations, attachment_dir)

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue
        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue
        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > _kb.KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{_kb.KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise _kb.ArtifactPreservationError(problem)
        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _kb._unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_review_artifact(resolved_src, dest, artifact)
        except Exception as exc:
            _discard_copies()
            if isinstance(exc, _kb.ArtifactPreservationError):
                raise
            raise _kb.ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True
    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
