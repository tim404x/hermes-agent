"""Task workspace lifecycle: scratch/dir/worktree resolution (incl. git worktree creation), post-completion cleanup with containment guards, worker tmux teardown and the first-use scratch-workspace tip.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from hermes_cli.worktree_ops import release_lsp_clients

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

_REMOVABLE_KINDS = ("scratch", "worktree")
_PROC_ROOT = Path("/proc")
_AGENT_HOST_UNIT_RE = re.compile(r"^hermes-(?:gateway|dashboard)(?:-[^/]*)?\.service$")
_DELETED_SUFFIX = " (deleted)"
_LIVE_CLEANUP_RETRY_INTERVAL_SECONDS = 30.0
_DatabaseKey = str | sqlite3.Connection
_LIVE_CLEANUP_LAST_RETRY: dict[tuple[_DatabaseKey, str], float] = {}
_LIVE_CLEANUP_AFTER: dict[_DatabaseKey, int] = {}
_FINISHED_CLEANUPS = {"removed", "gone", "preserved", "unmanaged", "shared", "active_children"}


def _path_key(path: Path | str | None) -> str:
    """Unicode-form-insensitive identity for a filesystem path.

    macOS hands back DECOMPOSED path strings (NFD: ``o`` + U+0308) for names the
    user typed in composed form (NFC: ``ö``) — a OneDrive/FileProvider path like
    ``OneDrive-Persönlich`` round-trips through ``git rev-parse --show-toplevel``
    as NFD while the DB row holds NFC. Raw ``Path`` equality then reports a real
    repo root as "not a repo" purely on Unicode form, so every path identity
    check here goes through this key.
    """
    return unicodedata.normalize("NFC", str(path)) if path is not None else ""

# Statuses after which a task no longer needs a workspace: a child stops
# needing its parent's handoff artifacts, and a sharer stops holding a dir.
_TERMINAL_STATUSES_SQL = "('done', 'archived', 'failed', 'cancelled')"

_ACTIVE_CHILDREN_SQL = (
    "SELECT 1 FROM task_links l "
    "JOIN tasks t ON t.id = l.child_id "
    f"WHERE l.parent_id = ? AND t.status NOT IN {_TERMINAL_STATUSES_SQL} "
    "LIMIT 1"
)

_WORKSPACE_ROW_SQL = (
    "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ? "
    f"AND status IN {_TERMINAL_STATUSES_SQL}"
)

_PENDING_LIVE_EVENTS_SQL = """
    SELECT task_id, id, payload FROM task_events WHERE kind = 'workspace_cleanup_deferred_live' AND id IN (
        SELECT MAX(id) FROM task_events WHERE kind IN ('workspace_cleanup_deferred_live', 'workspace_cleanup_retried')
        GROUP BY task_id
    )
"""


def _git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
    """``git -C repo_root args``; never raises on a non-zero exit.

    :func:`noninteractive_repo_git_env` (GHSA-7x36-8jrh-v4pw): the dispatcher runs ``worktree add``
    unattended, which executes the repo's hooks, ``core.fsmonitor`` and smudge filters.
    """
    from hermes_cli._subprocess_compat import FILTER_DISCOVERY_FAILED, noninteractive_repo_git_env
    env = noninteractive_repo_git_env(repo_root)
    if env is None:
        return subprocess.CompletedProcess(["git", "-C", str(repo_root), *args], 1, "", FILTER_DISCOVERY_FAILED)
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _has_active_children(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(_ACTIVE_CHILDREN_SQL, (task_id,)).fetchone() is not None


_OTHER_LIVE_PATHS_SQL = (
    "SELECT workspace_path FROM tasks "
    "WHERE id != ? AND workspace_path IS NOT NULL "
    f"AND status NOT IN {_TERMINAL_STATUSES_SQL}"
)
# Sibling boards mint their own ids, so the id being cleaned up here can
# name a different live task there. Do not exclude it.
_ANY_LIVE_PATHS_SQL = (
    "SELECT workspace_path FROM tasks "
    "WHERE workspace_path IS NOT NULL "
    f"AND status NOT IN {_TERMINAL_STATUSES_SQL}"
)


def _conn_uses_path(
    conn: sqlite3.Connection, task_id: str, key: str, *, exclude_task_id: bool = True
) -> bool:
    if exclude_task_id:
        rows = conn.execute(_OTHER_LIVE_PATHS_SQL, (task_id,)).fetchall()
    else:
        rows = conn.execute(_ANY_LIVE_PATHS_SQL).fetchall()
    for row in rows:
        other = row["workspace_path"]
        if not other:
            continue
        try:
            other_key = _path_key(Path(other).expanduser().resolve(strict=False))
        except OSError:
            continue
        if other_key == key:
            return True
    return False


def _sibling_board_db_files(conn: sqlite3.Connection) -> list[Path]:
    """Every other board's ``kanban.db``. Raises ``OSError`` when the set is unknown.

    ``kanban_db_path`` follows ``HERMES_KANBAN_DB`` and would collapse every
    slug onto the pinned file, so the scan uses the on-disk layout: the
    default board at ``<home>/kanban.db`` and named boards at
    ``<home>/kanban/boards/<slug>/kanban.db``.
    """
    current_file = _main_db_file(conn)
    current = Path(current_file).resolve() if current_file else None
    candidates = [_kb.kanban_home() / "kanban.db"]
    root = _kb.boards_root()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                candidates.append(child / "kanban.db")
    found: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if current is not None and resolved == current:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def _other_board_uses_path(db_file: Path, task_id: str, key: str) -> bool:
    uri = db_file.as_uri() + "?mode=ro"
    other = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        other.row_factory = sqlite3.Row
        return _conn_uses_path(other, task_id, key, exclude_task_id=False)
    finally:
        other.close()


def _workspace_in_use_by_other(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> Optional[str]:
    """Why *path* must be kept: ``"shared"``, ``"unknown"``, or None when free.

    ``gc``, completion and deferred parent cleanup used to ``rmtree`` a shared
    ``workspace_path`` as soon as one of its tasks went terminal. Compare the
    resolved path: a row may store ``~`` or a symlinked spelling.

    The connection covers one board. A ready task on a named board can point
    at the same directory. If that set of databases cannot be read, refuse
    the delete (``"unknown"``) and log the board DB that failed, so one broken
    sibling DB is not reported as a live sharer.
    """
    try:
        key = _path_key(Path(path).expanduser().resolve(strict=False))
    except OSError as exc:
        _kb._log.warning("Cannot resolve workspace %s for task %s: %s", path, task_id, exc)
        return "unknown"
    if not key:
        return None
    try:
        if _conn_uses_path(conn, task_id, key):
            return "shared"
    except sqlite3.Error as exc:
        _kb._log.warning("Cannot read live workspaces for task %s: %s", task_id, exc)
        return "unknown"
    try:
        siblings = _sibling_board_db_files(conn)
    except OSError as exc:
        _kb._log.warning("Cannot list kanban boards for task %s: %s", task_id, exc)
        return "unknown"
    for db_file in siblings:
        try:
            if _other_board_uses_path(db_file, task_id, key):
                return "shared"
        except (OSError, sqlite3.Error) as exc:
            _kb._log.warning(
                "Cannot read board db %s for task %s: %s", db_file, task_id, exc,
            )
            return "unknown"
    return None


def _defer_shared_workspace_cleanup(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> Optional[str]:
    """Skip removal and record why, when *path* may still be used by a live task."""
    reason = _workspace_in_use_by_other(conn, task_id, path)
    if reason is None:
        return None
    if reason == "shared":
        _kb._log.warning(
            "Deferring workspace cleanup for task %s: %s is still used by "
            "another non-terminal task",
            task_id, path,
        )
    else:
        _kb._log.warning(
            "Deferring workspace cleanup for task %s: cannot tell whether "
            "another task still uses %s",
            task_id, path,
        )
    _record_cleanup_event(conn, task_id, "workspace_cleanup_deferred_shared", {"path": str(path), "reason": reason})
    return reason


def _record_cleanup_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: dict
) -> None:
    """One INSERT on the board's autocommit connection, outside the task transition."""
    try:
        _kb._append_event(conn, task_id, kind, payload)
    except sqlite3.Error as exc:
        _kb._log.debug(
            "kanban workspace: failed to record %s for task %s: %s", kind, task_id, exc
        )


def _lexical_path(path: Path | str) -> Path:
    """Absolute, ``..``-collapsed, NFC form of *path* WITHOUT following symlinks."""
    return Path(_path_key(os.path.abspath(path)))


def _read_cgroup(pid: int) -> str:
    path = ""
    for line in (_PROC_ROOT / str(pid) / "cgroup").read_bytes().splitlines():
        fields = line.split(b":", 2)
        if len(fields) == 3:
            path = os.fsdecode(fields[2])
            if fields[:2] == [b"0", b""]:
                break
    return path


def _proc_references(directory: Path, map_filter: re.Pattern[bytes]) -> Iterator[tuple[str, str]]:
    for via in ("cwd", "root", "exe"):
        with contextlib.suppress(FileNotFoundError):
            yield via, os.readlink(directory / via)
    with contextlib.suppress(FileNotFoundError), os.scandir(directory / "fd") as entries:
        for entry in entries:
            with contextlib.suppress(FileNotFoundError):
                yield "fd", os.readlink(entry.path)
    maps = (directory / "maps").read_bytes()
    if map_filter.search(maps):
        for line in maps.split(b"\n"):
            fields = line.split(None, 5)
            if len(fields) == 6:
                yield "map", os.fsdecode(fields[5])


class _WorkspaceProbe:
    def __init__(self, paths: dict[str, Path | str]) -> None:
        self.roots: dict[str, set[str]] = {}
        for task_id, path in paths.items():
            for root in (os.path.realpath(path), str(_lexical_path(path))):
                self.roots.setdefault(root, set()).add(task_id)
        self.prefix = os.path.commonprefix(list(self.roots))
        self.map_filter = re.compile(b"|".join(re.escape(os.fsencode(root)) for root in self.roots))
        self.worker_units = {
            key: re.compile(rf"hermes-worker-kanban-{re.escape(key)}-run-[^/]+\.scope") for key in paths
        }

    def matches(self, directory: Path, pending: set[str]) -> dict[str, str]:
        """First reference per workspace: one process can hold several workspaces."""
        matches = {}
        with contextlib.suppress(OSError):
            for via, target in _proc_references(directory, self.map_filter):
                target = target.removesuffix(_DELETED_SUFFIX)
                while target and target != "/" and target.startswith(self.prefix):
                    for task_id in self.roots.get(target, ()):
                        if task_id in pending:
                            matches.setdefault(task_id, via)
                    target = target.rpartition(os.sep)[0]
                if len(matches) == len(pending):
                    break
        return matches

    def foreign(self, pid: int, current_group: str, pending: set[str]) -> Iterator[tuple[str, dict]]:
        directory = _PROC_ROOT / str(pid)
        matches = self.matches(directory, pending)
        if not matches:
            return
        cgroup = _read_cgroup(pid)
        if current_group and cgroup == current_group:
            return
        unit = cgroup.rsplit("/", 1)[-1]
        env = (directory / "environ").read_bytes().split(b"\0") if _AGENT_HOST_UNIT_RE.fullmatch(unit) else ()
        comm = (directory / "comm").read_bytes().removesuffix(b"\n").decode(errors="replace")
        for task_id, via in matches.items():
            if self.worker_units[task_id].fullmatch(unit) or os.fsencode(f"HERMES_KANBAN_TASK={task_id}") in env:
                continue
            yield task_id, dict(pid=pid, comm=comm, unit=unit, cgroup=cgroup, via=via)


def _foreign_workspace_holders_many(paths: dict[str, Path | str], *, limit: int = 10) -> dict[str, list[dict] | None]:
    """One procfs snapshot; None means enumeration failed, not that the root is free."""
    holders: dict[str, list[dict]] = {task_id: [] for task_id in paths}
    try:
        if not paths or limit <= 0 or not (_PROC_ROOT / "self").is_dir():
            return holders
        probe = _WorkspaceProbe(paths)
        pending = set(paths)
        current_pid = os.getpid()
        current_group = ""
        with contextlib.suppress(OSError):
            current_group = _read_cgroup(current_pid)
        with os.scandir(_PROC_ROOT) as entries:
            for entry in entries:
                if not entry.name.isdecimal() or (pid := int(entry.name)) == current_pid:
                    continue
                with contextlib.suppress(OSError):
                    for task_id, holder in probe.foreign(pid, current_group, pending):
                        holders[task_id].append(holder)
                        if len(holders[task_id]) == limit:
                            pending.remove(task_id)
                if not pending:
                    break
    except OSError:
        return {task_id: found or None for task_id, found in holders.items()}
    return holders


def _foreign_workspace_holders(task_id: str, path: Path | str, *, limit: int = 10) -> list[dict] | None:
    return _foreign_workspace_holders_many({task_id: path}, limit=limit)[task_id]


def _defer_live_workspace_cleanup(
    conn: sqlite3.Connection, task_id: str, path: Path, *, record: bool, holders: list[dict] | None,
    unknown_reason: str = "scan_failed",
) -> bool:
    if holders == []:
        return False
    log = _kb._log.warning if record else _kb._log.debug
    payload = {"path": str(path), "holders": holders or []}
    if holders is None:
        log("Deferring workspace cleanup for task %s: %s (%s)", task_id, path, unknown_reason)
        payload["reason"] = unknown_reason
    else:
        first = holders[0]
        log("Deferring workspace cleanup for task %s: %s held by pid %s (%s, %s)",
            task_id, path, first["pid"], first["unit"], first["comm"])
    if record:
        _record_cleanup_event(conn, task_id, "workspace_cleanup_deferred_live", payload)
    return True


def _managed_scratch_path_info(p: Path) -> tuple[bool, Optional[str]]:
    """Return whether *p* is managed scratch storage and the matching board.

    *p* must be strictly below a managed root both after resolving symlinks
    AND lexically (as spelled, without resolving). Resolved containment alone
    is not enough: when a root is itself a symlink to a broad directory
    (relocated storage, or a planted link), every path inside the link target
    would resolve "under" the root, so a scratch task naming such a path
    directly would get it rmtree'd. Tasks created through the root are spelled
    through it, so the lexical check keeps them managed. A root's lexical form
    is accepted both as configured and with its anchor (kanban home, or the
    override's parent) resolved, so a process spelling a symlinked home by its
    real path still matches; the managed ``kanban/.../workspaces`` components
    themselves are never resolved for the lexical check.
    """
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False, None
    p_lex = _lexical_path(p)
    # (resolved root, lexical spellings of the root, board)
    roots: list[tuple[Path, tuple[Path, ...], Optional[str]]] = []

    def _add_root(
        anchor: Path, anchor_real: Path, parts: tuple[str, ...], board: Optional[str]
    ) -> None:
        root = anchor.joinpath(*parts)
        with contextlib.suppress(OSError):
            roots.append((
                root.resolve(strict=False),
                (_lexical_path(root), _lexical_path(anchor_real.joinpath(*parts))),
                board,
            ))

    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        override_root = Path(override).expanduser()
        with contextlib.suppress(OSError):
            override_parent = override_root.parent
            _add_root(
                override_parent,
                override_parent.resolve(strict=False),
                (override_root.name,),
                None,
            )
    try:
        home = _kb.kanban_home()
        # Resolve the shared anchor once, not once per board root.
        home_real = home.resolve(strict=False)
    except OSError:
        home = None
    if home is not None:
        _add_root(home, home_real, ("kanban", "workspaces"), _kb.DEFAULT_BOARD)
        entries: list[Path] = []
        with contextlib.suppress(OSError):
            entries = list((home / "kanban" / "boards").resolve(strict=False).iterdir())
        for entry in entries:
            with contextlib.suppress(OSError):
                if entry.is_dir():
                    _add_root(
                        home, home_real, ("kanban", "boards", entry.name, "workspaces"), entry.name
                    )
    for root, lexical_roots, board in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root) and any(
                p_lex != lex and p_lex.is_relative_to(lex) for lex in lexical_roots
            ):
                return True, board
        except ValueError:
            continue
    return False, None


def _scratch_workspace(conn: sqlite3.Connection, task_id: str) -> Optional[Path]:
    """Expanded ``workspace_path`` when the task uses a scratch workspace, else ``None``."""
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
        return None
    return Path(row["workspace_path"]).expanduser()


def _is_managed_scratch_path(p: Path) -> bool:
    """True iff *p* is a STRICT descendant of a kanban-managed ``workspaces/``
    root (``HERMES_KANBAN_WORKSPACES_ROOT``, ``<kanban_home>/kanban/workspaces``,
    or ``<kanban_home>/kanban/boards/<slug>/workspaces``). A path equal to a
    root is not managed (deleting it would wipe every task's scratch dir);
    ``<kanban_home>/kanban``, ``.../logs`` and ``.../boards/<slug>`` hold
    Hermes' own DB and metadata. :func:`_cleanup_workspace` refuses
    ``rmtree`` outside managed storage — a board ``default_workdir`` on a real
    source tree paired with ``workspace_kind='scratch'`` would otherwise make
    task completion delete user data.

    See #28818.
    """
    return _managed_scratch_path_info(p)[0]


def _board_scratch_root(conn: sqlite3.Connection) -> Path | None:
    """Resolve implicit scratch paths from the open DB, not the caller's selected board."""
    filename = _main_db_file(conn)
    if not filename:
        return None
    database = Path(filename)
    if database == _kb.kanban_db_path().resolve() and (
        not os.environ.get("HERMES_KANBAN_DB") or os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT")
    ):
        return _kb.workspaces_root()
    if database == (_kb.kanban_home() / "kanban.db").resolve():
        return _kb.kanban_home() / "kanban" / "workspaces"
    if database.name == "kanban.db" and database.parent.parent == _kb.boards_root().resolve():
        return _kb.boards_root() / database.parent.name / "workspaces"
    return None


@dataclass(frozen=True)
class _WorkspaceCleanup:
    task_id: str
    kind: str
    path: Path
    branch_name: Optional[str]
    stored_path: str | None
    real_path: str

    @classmethod
    def from_row(cls, conn: sqlite3.Connection, row: sqlite3.Row) -> _WorkspaceCleanup | None:
        stored_path, kind, task_id = row["workspace_path"], row["workspace_kind"], row["id"]
        if stored_path:
            path = Path(stored_path).expanduser()
        elif kind == "scratch" and Path(task_id).name == task_id and task_id not in ("", ".."):
            root = _board_scratch_root(conn)
            if root is None:
                return None
            path = root / task_id
        else:
            return None
        return cls(task_id, kind, path, row["branch_name"], stored_path, os.path.realpath(path))

    def blocker(self, conn: sqlite3.Connection) -> Optional[str]:
        try:
            is_dir = stat.S_ISDIR(self.path.stat().st_mode)
        except (FileNotFoundError, NotADirectoryError):
            return "gone"
        except OSError:
            return "unknown"
        if not is_dir:
            return "unmanaged"
        if _has_active_children(conn, self.task_id):
            return "active_children"
        if self.kind == "scratch":
            if self.path.is_symlink() or not _is_managed_scratch_path(self.path):
                _kb._log.warning("Refusing to remove out-of-scratch workspace for task %s: %s", self.task_id, self.path)
                return "unmanaged"
        elif self.kind != "worktree" or self.path.is_symlink() or not (self.path / ".git").is_file():
            return "unmanaged"
        return _defer_shared_workspace_cleanup(conn, self.task_id, self.path)

    def revalidate(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute(_WORKSPACE_ROW_SQL, (self.task_id,)).fetchone()
        if row is None or self != self.from_row(conn, row):
            return "task_changed"
        return self.blocker(conn)

    def worktree_guard(self, conn: sqlite3.Connection, record_live: bool) -> str | None:
        """Check DB ownership first; no sibling DB walks between the final procfs scan and Git."""
        version = _board_revision(conn)
        if reason := self.revalidate(conn):
            return reason
        holders = _foreign_workspace_holders(self.task_id, self.path)
        unknown_reason = "scan_failed"
        if holders == [] and _board_revision(conn) != version:
            holders, unknown_reason = None, "board_changed_during_scan"
        if _defer_live_workspace_cleanup(conn, self.task_id, self.path, record=record_live,
                                         holders=holders, unknown_reason=unknown_reason):
            return "unknown" if holders is None else "live"
        return None


def _board_revision(conn: sqlite3.Connection) -> tuple[int, int]:
    """SQLite counts this connection's writes separately from commits on other connections."""
    return conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0]


def _cleanup_task_workspaces(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], *, record_live: bool = True,
) -> dict[str, str]:
    results: dict[str, str] = {}
    ready = []
    for row in rows:
        ws = _WorkspaceCleanup.from_row(conn, row)
        if ws is None or (not record_live and str(ws.path) != row["deferred_path"]):
            results[row["id"]] = "unmanaged"
            continue
        reason = ws.blocker(conn)
        if reason:
            results[ws.task_id] = reason
        else:
            release_lsp_clients(str(ws.path))
            ready.append(ws)
    batch = _foreign_workspace_holders_many({ws.task_id: ws.path for ws in ready}) if len(ready) > 1 else {}
    for ws in ready:
        holders = batch[ws.task_id] if batch else _foreign_workspace_holders(ws.task_id, ws.path)
        if _defer_live_workspace_cleanup(conn, ws.task_id, ws.path, record=record_live, holders=holders):
            results[ws.task_id] = "unknown" if holders is None else "live"
            continue
        if ws.kind == "worktree":
            results[ws.task_id] = _cleanup_worktree_workspace(
                ws.task_id, str(ws.path), ws.branch_name, guard=partial(ws.worktree_guard, conn, record_live),
            )
        elif blocker := ws.revalidate(conn):
            results[ws.task_id] = blocker
        else:
            try:
                shutil.rmtree(ws.path)
                results[ws.task_id] = "removed"
            except OSError:
                results[ws.task_id] = "error"
    return results


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.
    Called from :func:`complete_task` after the transaction commits; best-effort
    so cleanup never blocks completion. ``scratch`` is removed; ``worktree``
    only when provably free of work (clean tree, every commit reachable from a
    remote-tracking ref); ``dir`` is intentionally preserved."""
    try:
        row = conn.execute(_WORKSPACE_ROW_SQL, (task_id,)).fetchone()
        if not row:
            return
        kind = row["workspace_kind"]
        if kind in _REMOVABLE_KINDS:
            if _has_active_children(conn, task_id):
                return
            if kind == "worktree":
                _cleanup_worker_tmux(conn, task_id)
            _cleanup_task_workspaces(conn, [row])
            if kind == "scratch" and row["workspace_path"]:
                _cleanup_worker_tmux(conn, task_id)
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _cleanup_worktree_workspace(
    task_id: str, path: str, branch_name: Optional[str] = None, *,
    guard: Callable[[], str | None] | None = None,
) -> str:
    """Remove a finished task's linked git worktree when it holds no work.
    Mirrors the CLI startup pruner (``cli._prune_stale_worktrees``): removal
    requires a clean tree AND every commit reachable from a remote-tracking
    ref; any doubt (dirty, unpushed, unresolvable repo, failing git) preserves
    it. The auto-generated ``wt/<task-id>`` branch is deleted with it; custom
    branches are kept. Best-effort."""
    try:
        from hermes_cli.worktree_ops import _worktree_has_unpushed_commits, _worktree_is_dirty
        wp = Path(path).expanduser()
        if not wp.is_dir():
            return "gone"
        directories = _git_checkout_dirs(wp)
        if directories is None:
            return "unknown"
        checkout, common = directories
        if _path_key(checkout) == _path_key(common) or common.name != ".git":
            return "unmanaged"
        repo_root = common.parent
        if _path_key(wp.resolve(strict=False)) == _path_key(repo_root.resolve(strict=False)):
            return "unmanaged"
        dirty = _worktree_is_dirty(str(wp), str(repo_root), on_error=None)
        if dirty is None:
            return "unknown"
        has_work = dirty or _worktree_has_unpushed_commits(str(wp), on_error=None)
        if has_work is None:
            return "unknown"
        if has_work:
            _kb._log.info(
                "Preserving worktree for task %s: dirty or unpushed work at %s",
                task_id, wp,
            )
            return "preserved"
        # Windows cannot delete a directory while this process has its current
        # directory inside it. Completed workers normally run from their own
        # linked worktree, so move this process back to the main checkout
        # before asking Git to remove the worktree.
        worktree_path = wp.resolve(strict=False)
        try:
            cwd = Path.cwd().resolve(strict=False)
        except OSError:
            # cwd was already deleted (a scratch-kind child's own workspace is
            # rmtree'd before this deferred parent cleanup runs, #33774). A
            # dead cwd cannot hold the worktree open, so leaving it is safe.
            cwd = None
        if cwd is None or cwd == worktree_path or cwd.is_relative_to(worktree_path):
            try:
                os.chdir(repo_root)
            except OSError as exc:
                _kb._log.warning(
                    "Preserving worktree for task %s: cannot leave %s for %s: %s",
                    task_id, cwd or "<deleted cwd>", repo_root, exc,
                )
                return "error"
        # No --force: git's own dirty guard re-verifies at removal time, so if
        # the tree became dirty since our check (TOCTOU) removal fails safe.
        if guard is None:
            release_lsp_clients(str(worktree_path))
        for attempt in range(2):
            if attempt:
                time.sleep(0.1)
            reason = guard() if guard else ("live" if _foreign_workspace_holders(task_id, wp) != [] else None)
            if reason:
                return reason
            result = _git(repo_root, "worktree", "remove", str(wp), timeout=60)
            if result.returncode == 0:
                break
        else:
            _kb._log.warning(
                "git worktree remove failed for task %s at %s: %s",
                task_id, wp, (result.stderr or result.stdout or "").strip(),
            )
            return "error"
        _kb._log.debug("Removed worktree workspace: %s", wp)
        branch = (branch_name or "").strip() or f"wt/{task_id}"
        if branch.startswith("wt/"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _git(repo_root, "branch", "-D", branch, timeout=30)
        return "removed"
    except Exception:
        return "error"


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Run the deferred cleanup of any parent scratch/worktree workspace whose
    children are now all done/archived/failed/cancelled (called after each
    child completes).

    See #33774.
    """
    try:
        parents = conn.execute(
            "SELECT t.id, t.workspace_kind, t.workspace_path, t.branch_name FROM task_links l "
            "JOIN tasks t ON t.id = l.parent_id WHERE l.child_id = ? "
            f"AND t.workspace_kind IN ('scratch','worktree') AND t.status IN {_TERMINAL_STATUSES_SQL}",
            (task_id,),
        ).fetchall()
        _cleanup_task_workspaces(conn, parents)
    except Exception:
        pass  # best-effort


def _retry_page(conn: sqlite3.Connection, board: _DatabaseKey, recent: list[str], limit: int) -> list[sqlite3.Row]:
    """Walk event ids in order; long-lived holders in a full page must not starve the next page."""
    if limit <= 0:
        return []
    after = _LIVE_CLEANUP_AFTER.get(board, 0)
    query = (
        "SELECT t.id, t.workspace_kind, t.workspace_path, t.branch_name, e.id AS event_id, "
        "json_extract(e.payload, '$.path') AS deferred_path "
        f"FROM ({_PENDING_LIVE_EVENTS_SQL}) e "
        f"JOIN tasks t ON t.id = e.task_id WHERE t.status IN {_TERMINAL_STATUSES_SQL} "
        "AND t.id NOT IN (SELECT value FROM json_each(?)) AND e.id > ? ORDER BY e.id LIMIT ?"
    )
    exclude = json.dumps(recent)
    rows = conn.execute(query, (exclude, after, limit)).fetchall()
    if after and not rows:
        rows = conn.execute(query, (exclude, 0, limit)).fetchall()
    if len(rows) == limit:
        _LIVE_CLEANUP_AFTER[board] = rows[-1]["event_id"]
    else:
        _LIVE_CLEANUP_AFTER.pop(board, None)
    return rows


def retry_deferred_workspace_cleanups(conn: sqlite3.Connection, *, limit: int = 20) -> list[str]:
    """Retry terminal workspaces still queued by their latest live-cleanup event."""
    now = time.monotonic()
    board = _main_db_file(conn) or conn
    recent = []
    for key, last in _LIVE_CLEANUP_LAST_RETRY.copy().items():
        if now - last >= _LIVE_CLEANUP_RETRY_INTERVAL_SECONDS:
            _LIVE_CLEANUP_LAST_RETRY.pop(key, None)
        elif key[0] == board:
            recent.append(key[1])
    rows = _retry_page(conn, board, recent, limit)
    _LIVE_CLEANUP_LAST_RETRY.update({(board, row["id"]): now for row in rows})
    results = _cleanup_task_workspaces(conn, rows, record_live=False)
    finished = [row for row in rows if results[row["id"]] in _FINISHED_CLEANUPS]
    if finished:
        with _kb.write_txn(conn):
            for row in finished:
                task_id = row["id"]
                latest = conn.execute(
                    "SELECT id FROM task_events WHERE task_id=? AND kind IN "
                    "('workspace_cleanup_deferred_live','workspace_cleanup_retried') ORDER BY id DESC LIMIT 1", (task_id,),
                ).fetchone()
                if latest and latest[0] == row["event_id"]:
                    _kb._append_event(conn, task_id, "workspace_cleanup_retried", {"result": results[task_id]})
                _LIVE_CLEANUP_LAST_RETRY.pop((board, task_id), None)
    return [row["id"] for row in finished if results[row["id"]] == "removed"]


def gc_archived_workspaces(conn: sqlite3.Connection) -> int:
    """Sweep archived leftovers not already owned by the live-retry queue."""
    rows = conn.execute(
        "SELECT id, workspace_kind, workspace_path, branch_name FROM tasks WHERE status = 'archived' "
        f"AND workspace_kind IN ('scratch','worktree') AND id NOT IN (SELECT task_id FROM ({_PENDING_LIVE_EVENTS_SQL}))"
    ).fetchall()
    return sum(result == "removed" for result in _cleanup_task_workspaces(conn, rows).values())


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{row['assignee']}"
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5,
            check=False,
        )
        if out.stdout.strip() == "1":
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True, timeout=5, check=False)
            _kb._log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"


_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're removed after completion or archive "
    "once no longer in use. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return _kb.kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip was already emitted on this install.
    Best-effort — any error re-emits, the safer failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent. Best-effort:
    a failure means the tip may appear once more, preferable to crashing dispatch."""
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip once per install, right after a
    scratch workspace is materialized. No-op for ``worktree``/``dir`` (preserved
    by design) and once the sentinel exists."""
    if (workspace_kind or "scratch") != "scratch" or _scratch_tip_shown():
        return
    try:
        _kb._log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def _git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel containing ``path``, or ``None`` if not in a repo."""
    out = _kb._git_out(path, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out).expanduser().resolve()
    except Exception:
        return Path(out).expanduser()


def _git_branch_exists(repo_root: Path, branch_name: str) -> bool:
    try:
        result = _git(repo_root, "show-ref", "--verify", f"refs/heads/{branch_name}", timeout=30)
    except Exception:
        return False
    return result.returncode == 0


def _git_abs_path(path: Path, flag: str) -> Optional[Path]:
    out = _kb._git_out(path, "rev-parse", "--path-format=absolute", flag)
    return Path(out).expanduser().resolve(strict=False) if out else None


def _git_common_dir(path: Path) -> Optional[Path]:
    return _git_abs_path(path, "--git-common-dir")


def _git_current_branch(path: Path) -> Optional[str]:
    return _kb._git_out(path, "branch", "--show-current")


def _git_checkout_dirs(path: Path) -> tuple[Path, Path] | None:
    out = _kb._git_out(
        path, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"
    )
    lines = out.splitlines() if out else []
    if len(lines) != 2:
        return None
    return Path(lines[0]).resolve(strict=False), Path(lines[1]).resolve(strict=False)


def _is_linked_worktree_checkout(path: Path) -> bool:
    """True when *path* is a linked worktree (git-dir differs from common-dir)."""
    directories = _git_checkout_dirs(path)
    return directories is not None and _path_key(directories[0]) != _path_key(directories[1])


def _nearest_existing_path(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _repo_root_for_worktree_target(path: Path) -> Optional[Path]:
    current = _nearest_existing_path(path).resolve(strict=False)
    while True:
        repo_root = _git_toplevel(current)
        if repo_root is not None:
            return repo_root
        if current == current.parent:
            return None
        current = current.parent


def _ensure_git_worktree(repo_root: Path, target: Path, branch_name: str) -> None:
    """Materialize ``target`` as a linked git worktree under ``repo_root``."""
    target = target.expanduser()
    repo_common = _git_common_dir(repo_root)
    if target.exists() and repo_common is not None and _path_key(_git_common_dir(target)) == _path_key(repo_common):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if _git_branch_exists(repo_root, branch_name):
        args = ["worktree", "add", str(target), branch_name]
    else:
        args = ["worktree", "add", "-b", branch_name, str(target), "HEAD"]
    result = _git(repo_root, *args, timeout=60)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"git worktree add failed for {target} on branch {branch_name}: {stderr}"
        )


def _anchored_worktree(repo_root: Path, task_id: str, branch_name: str) -> tuple[Path, str]:
    """Materialize the canonical ``<repo>/.worktrees/<task-id>`` worktree."""
    target = repo_root / ".worktrees" / task_id
    _ensure_git_worktree(repo_root, target, branch_name)
    return target, branch_name


def _resolve_worktree_workspace(task: Task, *, board: Optional[str] = None) -> tuple[Path, str]:
    """Resolve + materialize a linked git worktree for ``task``. With no
    ``task.workspace_path`` the anchor is the board's ``default_workdir`` so
    every worktree lands under a board-owned repo (``<repo>/.worktrees/<id>``)
    instead of the dispatcher's incidental CWD (whatever dir the gateway was
    launched from); with no anchor configured we fail loudly rather than guess."""
    branch_name = (task.branch_name or "").strip() or f"wt/{task.id}"
    if not task.workspace_path:
        board_slug = board if board else _kb.get_current_board()
        board_default = (_kb.read_board_metadata(board_slug).get("default_workdir") or "").strip()
        if not board_default:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but no workspace_path, "
                f"and board {board_slug!r} has no default_workdir set. Set a board "
                "default workdir (a git repo) or create the task with "
                "--workspace worktree:<absolute-repo-path>."
            )
        anchor = Path(board_default).expanduser()
        if not anchor.is_absolute():
            raise ValueError(
                f"board {board_slug!r} default_workdir {board_default!r} is not "
                "absolute; use an absolute path to a git repo"
            )
        repo_root = _git_toplevel(anchor)
        if repo_root is None:
            raise ValueError(
                f"task {task.id} has workspace_kind=worktree but board "
                f"{board_slug!r} default_workdir {board_default!r} is not inside a git repo"
            )
        return _anchored_worktree(repo_root, task.id, branch_name)

    requested = Path(task.workspace_path).expanduser()
    if not requested.is_absolute():
        raise ValueError(
            f"task {task.id} has non-absolute worktree path "
            f"{task.workspace_path!r}; use an absolute path"
        )
    requested_resolved = requested.resolve(strict=False)

    if requested.exists() and _is_linked_worktree_checkout(requested):
        actual_branch = _git_current_branch(requested)
        if actual_branch == branch_name:
            return requested_resolved, actual_branch
        # The requested path is an existing checkout of a DIFFERENT task's
        # branch (decompose children inherit the root's workspace_path
        # verbatim, so siblings all point here). Reusing it would run this task
        # on the other task's branch — silent cross-task provenance corruption,
        # unsafe under concurrency — so fall back to our own worktree.
        fallback_root = _repo_root_for_worktree_target(requested.parent)
        if fallback_root is not None:
            fallback = fallback_root / ".worktrees" / task.id
            if _path_key(fallback.resolve(strict=False)) != _path_key(requested_resolved):
                _ensure_git_worktree(fallback_root, fallback, branch_name)
                return fallback.resolve(strict=False), branch_name
        # No repo to anchor a fallback on (or the occupied path IS this task's
        # own canonical worktree): keep the legacy reuse rather than fail dispatch.
        return requested_resolved, actual_branch or branch_name

    repo_root = _git_toplevel(requested)
    if repo_root is not None and _path_key(requested_resolved) == _path_key(repo_root):
        return _anchored_worktree(repo_root, task.id, branch_name)

    repo_root = _repo_root_for_worktree_target(requested.parent)
    if repo_root is None:
        raise ValueError(
            f"task {task.id} worktree path {task.workspace_path!r} is not inside a git repo "
            "and does not point at a git repo root"
        )
    _ensure_git_worktree(repo_root, requested, branch_name)
    return requested, branch_name


def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    ``scratch``: ``<board-root>/workspaces/<id>/`` — path-stable across the
    dispatcher and every profile worker. ``dir``: ``workspace_path``, created
    if missing; MUST be absolute (relative paths would resolve against the
    dispatcher's CWD — confused-deputy traversal). ``worktree``: a linked git
    worktree; a repo-root ``workspace_path`` anchors ``<repo>/.worktrees/<id>``,
    a concrete path is created/reused, none -> the board's ``default_workdir``
    (raises if unset rather than guessing). Persist via ``set_workspace_path``.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "worktree":
        return _resolve_worktree_workspace(task, board=board)[0]
    if kind == "scratch" and not task.workspace_path:
        p = _kb.workspaces_root(board=board) / task.id
    elif kind == "scratch":
        # Legacy explicit-path scratch tasks get the same absolute-path guard
        # as dir: — same threat model.
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; workspace paths must be absolute"
            )
    elif kind == "dir":
        if not task.workspace_path:
            raise ValueError(f"task {task.id} has workspace_kind=dir but no workspace_path")
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
    else:
        raise ValueError(f"unknown workspace_kind: {kind}")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _set_task_column(conn: sqlite3.Connection, task_id: str, column: str, value: str) -> None:
    with _kb.write_txn(conn):
        conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))


def set_workspace_path(conn: sqlite3.Connection, task_id: str, path: Path | str) -> None:
    _set_task_column(conn, task_id, "workspace_path", str(path))


def set_branch_name(conn: sqlite3.Connection, task_id: str, branch_name: str) -> None:
    _set_task_column(conn, task_id, "branch_name", str(branch_name))


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
from hermes_cli.kanban_db_connect import _main_db_file
