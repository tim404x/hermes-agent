"""Shared fleet registry and the deliberately narrow PRIORITIES.md grammar."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


class Refused(ValueError):
    """A bounded, operator-facing reason (never task prose or file contents)."""


def _read_local(path: Path) -> str:
    limit = 1024 * 1024
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('not a regular file')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('file too large')
    return raw.decode('utf-8-sig')


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError('absolute path required')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute path required')
    return path


def real_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve()


@dataclass(frozen=True)
class Project:
    key: str
    root: Path
    real_root: Path
    project_id: str | None
    priorities: Path
    board_db: Path

    @classmethod
    def parse(cls, key: str, entry: dict, home: Path) -> Project:
        required = {'workspace_root', 'priorities_file', 'board_db'}
        if not key or not isinstance(entry, dict) or not required <= entry.keys() <= required | {'project_id'}:
            raise ValueError('invalid project mapping')
        project_id = entry.get('project_id')
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise ValueError('invalid project id')
        root = _absolute(entry['workspace_root'])
        board = real_path(_absolute(entry['board_db']))
        if board_home(board) != home.resolve():
            raise ValueError('authority must be a board in this fleet')
        return cls(key, root, real_path(root), project_id, _absolute(entry['priorities_file']),
                   board)

    def matches(self, project_id: str | None, workspace: Path | None) -> bool:
        if self.project_id is not None and self.project_id == project_id:
            return True
        if workspace is None:
            return False
        # Include intermediate symlink targets: alias/out may enter the guarded
        # tree via alias and leave it again via out. That is a refusal, not opt-out.
        for part in (workspace, *workspace.parents):
            if part.is_relative_to(self.root) or part.is_relative_to(self.real_root):
                return True
            try:
                if part.resolve().is_relative_to(self.real_root):
                    return True
            except (OSError, RuntimeError, ValueError):
                continue
        return False


def board_home(db: Path | None) -> Path | None:
    if db is None or db.name != 'kanban.db':
        return None
    parent = db.parent
    if parent.parent.name == 'boards' and parent.parent.parent.name == 'kanban':
        return parent.parent.parent.parent
    return parent


def implicit_workspace(db: Path | None, kind: str, task_id: str) -> Path | None:
    """Resolve board-owned paths from SQLite main, including the default board's legacy layout."""
    home = board_home(db)
    if db is None or home is None:
        return None
    if kind == 'scratch':
        directory = home / 'kanban' if db.parent == home else db.parent
        return directory / 'workspaces' / task_id
    if kind != 'worktree':
        return None
    directory = db.parent if db.parent != home else home / 'kanban' / 'boards' / 'default'
    try:
        meta = json.loads(_read_local(directory / 'board.json'))
    except FileNotFoundError:
        return None
    anchor = meta.get('default_workdir') if isinstance(meta, dict) else None
    return Path(anchor).expanduser() if anchor else None


def load_projects(home: Path) -> tuple[Project, ...]:
    path = home / 'kanban' / 'guarded-projects.json'
    try:
        try:
            raw = _read_local(path)
        except FileNotFoundError:
            if not path.is_symlink():
                return ()
            raise
        entries = json.loads(raw)
        if not isinstance(entries, dict):
            raise ValueError('expected project mappings')
        return tuple(Project.parse(key, entry, home) for key, entry in entries.items())
    except (OSError, RuntimeError, ValueError) as exc:
        raise Refused('invalid_guard_registry') from exc


def _ranked_rows(text: str) -> list[str]:
    lines = []
    fence = ''
    for line in re.sub(r'<!--.*?(?:-->|\Z)', '', text, flags=re.DOTALL).splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker and not fence:
            fence = marker[1]
        elif fence and line.strip().startswith(fence) and not line.strip().strip(fence[0]):
            fence = ''
        else:
            lines.append('' if fence else line)
    headings = [index for index, line in enumerate(lines)
                if re.fullmatch(r'## Ranked(?: \([^()]*\))?\s*', line)]
    if len(headings) != 1:
        return []
    rows = []
    divider_seen = False
    for line in lines[headings[0] + 1:]:
        if not rows and not line.strip():
            continue
        if not rows and not divider_seen and line == '---':
            divider_seen = True
            continue
        if not (line.startswith('|') and line.rstrip().endswith('|')):
            break
        rows.append(line.rstrip())
    return rows


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in re.split(r'(?<!\\)\|', line[1:-1])]


def ranked(project: Project, task_id: str) -> bool:
    try:
        rows = _ranked_rows(_read_local(project.priorities))
    except (OSError, ValueError) as exc:
        raise Refused('priorities_unavailable') from exc
    if len(rows) < 2:
        return False
    header, divider = _cells(rows[0]), _cells(rows[1])
    if (header.count('#') != 1 or header.count('Card') != 1 or len(divider) != len(header)
            or not all(re.fullmatch(r':?-+:?', cell) for cell in divider)):
        return False
    for line in rows[2:]:
        cells = _cells(line)
        if len(cells) != len(header) or not re.fullmatch(r'[1-9][0-9]*', cells[header.index('#')]):
            continue
        ids = [part.strip() for part in cells[header.index('Card')].split(',')]
        if task_id in ids and all(re.fullmatch(r't_[0-9a-f]{8,}', part) for part in ids):
            return True
    return False
