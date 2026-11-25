"""Project reservations live and die with running/review status in one board DB."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from hermes_cli.kanban_admission.policy import Project, Refused, board_home, implicit_workspace, load_projects, ranked, real_path


RESOURCE_CLASSES = ('cpu', 'data', 'gpu')
_CLASS_CAP = {'gpu': 3, 'data': 1}
_FIELDS = 'id, project_id, workspace_kind, workspace_path, resource_class, write_scopes'


def encode_declaration(resource_class: str | None, write_scopes: list[str] | None) -> str | None:
    if resource_class is not None and resource_class not in RESOURCE_CLASSES:
        raise ValueError('resource_class must be cpu, data or gpu')
    if write_scopes is None:
        return None
    if (not isinstance(write_scopes, list) or len(write_scopes) > 64
            or not all(isinstance(path, str) and path.strip() and len(path) <= 4096 for path in write_scopes)):
        raise ValueError('write_scopes must be a list of at most 64 nonblank paths (or [] for read-only)')
    return json.dumps(write_scopes)


def _write_paths(raw: str | None, project: Project) -> tuple[Path, ...]:
    if raw is None:
        raise Refused('missing_write_scopes')
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError('not a list')
        encode_declaration(None, items)
        paths = []
        for item in items:
            path = Path(item)
            if '..' in path.parts or item.startswith('~') or any(char in item for char in '*?[]\x00'):
                raise ValueError('not a literal project path')
            real = real_path(project.real_root / path)
            if not real.is_relative_to(project.real_root):
                raise ValueError('scope escapes project')
            paths.append(real)
        return tuple(dict.fromkeys(paths))
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        raise Refused('invalid_write_scope') from exc


@dataclass(frozen=True)
class Lane:
    resource: str | None
    writes: tuple[Path, ...]

    @classmethod
    def from_card(cls, row: sqlite3.Row, project: Project, *, legacy: bool = False) -> Lane:
        resource = row['resource_class']
        if resource not in RESOURCE_CLASSES:
            if not legacy:
                raise Refused('missing_resource_class')
            resource = None
        try:
            writes = _write_paths(row['write_scopes'], project)
        except Refused:
            if not legacy:
                raise
            writes = (project.real_root,)
        return cls(resource, writes)

    def conflicts(self, other: Lane) -> bool:
        return any(left.is_relative_to(right) or right.is_relative_to(left)
                   for left in self.writes for right in other.writes)


def _locations(row: sqlite3.Row, db: Path | None) -> tuple[Path, ...]:
    path = (Path(row['workspace_path']).expanduser() if row['workspace_path'] else
            implicit_workspace(db, row['workspace_kind'], row['id']))
    paths = [path] if path is not None else []
    if path is not None and row['workspace_kind'] == 'worktree':
        # A checkout anchor can materialize under .worktrees instead of at
        # the requested path. Include the resolver's possible fallback, without running git.
        real = real_path(path)
        for ancestor in (real, *real.parents):
            if (ancestor / '.git').exists():
                paths.append(ancestor / '.worktrees' / row['id'])
                if ancestor != real or not (ancestor / '.git').is_file():
                    break
    return tuple(dict.fromkeys(paths))


def _matches(project: Project, row: sqlite3.Row, paths: tuple[Path, ...]) -> bool:
    return project.matches(row['project_id'], None) or any(project.matches(None, path) for path in paths)


def _occupied(conn: sqlite3.Connection, db: Path | None, project: Project, task_id: str) -> Iterator[Lane]:
    rows = conn.execute(
        f'SELECT {_FIELDS}, r.project AS reservation, r.resource AS resource, r.paths AS paths '
        'FROM tasks LEFT JOIN task_admission_reservations r ON r.task_id = tasks.id '
        "WHERE tasks.status IN ('running','review') AND tasks.id != ?", (task_id,),
    )
    for row in rows:
        if row['reservation'] == project.key:
            yield Lane(row['resource'], tuple(Path(path) for path in json.loads(row['paths'])))
        elif _matches(project, row, _locations(row, db)):
            yield Lane.from_card(row, project, legacy=True)


def _project_for(conn, row, projects, paths) -> Project | None:
    matches = [project for project in projects if _matches(project, row, paths)]
    existing = conn.execute('SELECT project FROM task_admission_reservations WHERE task_id = ?', (row['id'],)).fetchone()
    if (existing and any(project.key == existing['project'] for project in projects)
            and not any(project.key == existing['project'] for project in matches)):
        raise Refused('reservation_project_changed')
    if not matches:
        return None
    if len(matches) != 1:
        raise Refused('ambiguous_project')
    return matches[0]


def _reserve(conn: sqlite3.Connection, db: Path | None, row: sqlite3.Row, projects: tuple[Project, ...]) -> None:
    paths = _locations(row, db)
    project = _project_for(conn, row, projects, paths)
    if project is None:
        return
    if db != project.board_db:
        raise Refused('wrong_project_board')
    if row['workspace_kind'] == 'worktree' and not row['workspace_path']:
        raise Refused('explicit_worktree_required')
    if not paths or any(not path.is_absolute() or '..' in path.parts or not real_path(path).is_relative_to(project.real_root)
                        for path in paths):
        raise Refused('workspace_outside_project')
    if not ranked(project, row['id']):
        raise Refused('not_ranked')
    wanted = Lane.from_card(row, project)
    active = list(islice(_occupied(conn, db, project, row['id']), 6))
    if len(active) >= 6:
        raise Refused('project_card_cap')
    cap = _CLASS_CAP.get(wanted.resource)
    if cap is not None and sum(lane.resource in (None, wanted.resource) for lane in active) >= cap:
        raise Refused(f'{wanted.resource}_lane_cap')
    if any(wanted.conflicts(lane) for lane in active):
        raise Refused('write_scope_busy')
    conn.execute('INSERT OR REPLACE INTO task_admission_reservations (task_id, project, resource, paths) '
                 'VALUES (?, ?, ?, ?)', (row['id'], project.key, wanted.resource, json.dumps(list(map(str, wanted.writes)))))


def admit(conn: sqlite3.Connection, task_id: str, source: str, *, home: Path,
          claim: bool = False, expect_assignee: str | None = None) -> str | None:
    """Called inside BEGIN IMMEDIATE before the status CAS. Never creates a workspace or opens another board."""
    try:
        filename = next(db['file'] for db in conn.execute('PRAGMA database_list') if db['name'] == 'main')
        db = Path(filename).resolve() if filename else None
        authority_home = board_home(db)
        projects = load_projects(home)
        if authority_home is not None and authority_home != home.resolve():
            projects += load_projects(authority_home)
        if not projects:
            return None
        row = conn.execute(f'SELECT {_FIELDS}, claim_lock, assignee FROM tasks WHERE id = ? AND status = ?',
                           (task_id, source)).fetchone()
        if not row or (claim and row['claim_lock'] is not None) or (expect_assignee is not None and row['assignee'] != expect_assignee):
            return None
        _reserve(conn, db, row, projects)
    except Refused as exc:
        reason = str(exc)
    except (OSError, TypeError, ValueError, RuntimeError):
        reason = 'invalid_admission_path'
    else:
        reason = None
    conn.execute('UPDATE tasks SET admission_reason = ? WHERE id = ? AND status = ? AND admission_reason IS NOT ?',
                 (reason, task_id, source, reason))
    return reason
