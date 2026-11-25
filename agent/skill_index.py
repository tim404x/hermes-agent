"""One cached walk per skills root, shared by the guards and lookups that run on every tool call.

The skill-lock guard (``tools.skill_lock``, every terminal / write_file / patch call) and
``skill_view``'s candidate lookup (``tools.skills_tool._collect_skill_candidates``) each walked the
whole skills tree per call: ~16k files on Tim's profile, 70-330 ms of GIL-held Python per call. With
~30 tool threads busy in the gateway they starved its asyncio loop into >10 s stalls, and the
liveness watchdog killed the gateway (24 Nov 2026, ``loop stalled >10.0s`` then exit 75).

Here every root is walked ONCE (the shared pruning: EXCLUDED_SKILL_DIRS never, a support dir beside a
``SKILL.md`` never) and the result is reused until the tree changes. The check that it has not
changed is exact and stat-cheap (~1 ms for ~300 dirs): every directory the walk listed is ``stat``-ed
and compared on (dev, inode, mtime_ns, ctime_ns). A directory gains, loses or renames an entry only
by moving its mtime/ctime, and ctime cannot be set back by ``touch``/``rsync -t``. A moved directory
is re-listed: when only names the index ignores changed (``.usage.json`` rewrites in the root), the
index stays; otherwise the root is rebuilt. Symlinks the walk could not enter (dangling, or naming a
file) are watched through their target. A filesystem timestamp comes from a coarse clock, so a
second change inside the same tick as a listing leaves the stat equal: a directory whose stat is
younger than ``RACY_NS`` at listing time is re-listed on every check until it has aged past it (git's
"racily clean" rule). An in-process writer (skill_manage, hub install, curator archive/restore)
also bumps :func:`bump_generation`, which rebuilds every root on its next use.

Concurrent callers coalesce: a check that STARTED after a caller arrived answers for it, so 30
threads arriving together pay for about one check, not 30 (single-flight, under one lock).

Each root is indexed under the spelling the caller passed (paths in answers are built from it), so
profile scoping (``get_all_skills_dirs`` under the HERMES_HOME contextvar), external dirs and
project roots each get their own entry.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS

# A stat younger than this at the moment it was read can still change without moving (coarse
# filesystem clock): such a directory is re-listed on every check until it ages past it.
RACY_NS = 2_000_000_000
# Roots kept resident (LRU): one per profile skills dir, external dir and project skills dir in use.
_MAX_ROOTS = 64

_lock = threading.Lock()
_roots: OrderedDict[str, _RootIndex] = OrderedDict()
_generation = 0


def bump_generation() -> None:
    """An in-process write to a skills tree landed: rebuild every root on its next use. The stat
    check would see the write too; this also covers a write inside the same timestamp tick."""
    global _generation
    with _lock:
        _generation += 1


def clear() -> None:
    """Test hook: drop every cached root."""
    with _lock:
        _roots.clear()


def stat_key(path) -> Optional[tuple[int, int, int, int]]:
    """(dev, inode, mtime_ns, ctime_ns) of *path* (links followed), None when it cannot be stat-ed."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns)


def is_racy(key, read_at_ns: int) -> bool:
    """Whether a stat read at *read_at_ns* is too young to prove a later same-tick change would move it."""
    return key is not None and max(key[-2], key[-1]) >= read_at_ns - RACY_NS


class _Listing:
    """One directory as the walk sees it. ``relevant`` is everything the index depends on: a change
    to anything else in the directory (a sidecar file rewrite) leaves the index valid."""

    __slots__ = ("key", "links", "marker", "mds", "read_at", "relevant", "subdirs")

    def __init__(self, path: str) -> None:
        self.read_at = time.time_ns()
        self.key = stat_key(path)  # BEFORE the listing: a change in between moves the next check's stat
        with os.scandir(path) as listing:
            entries = list(listing)
        self.marker: Optional[tuple[bool, bool, bool]] = None  # SKILL.md: (is_dir, is_link, exists)
        subdirs, links, mds = [], [], []
        for entry in entries:
            name = entry.name
            try:
                is_link = entry.is_symlink()
            except OSError:
                is_link = False
            try:
                is_dir = entry.is_dir()  # follows links, as os.walk(followlinks=True) does
            except OSError:
                is_dir = False
            if name == "SKILL.md":
                self.marker = (is_dir, is_link, is_dir or not is_link or os.path.exists(entry.path))
            elif name.endswith(".md"):
                mds.append(name)
            if is_dir:
                if name not in EXCLUDED_SKILL_DIRS:
                    subdirs.append((name, is_link))
            elif is_link:
                links.append(name)  # dangling, or naming a file: watched through its target
        self.subdirs, self.links, self.mds = sorted(subdirs), sorted(links), sorted(mds)
        self.relevant = (self.marker, tuple(self.subdirs), tuple(self.links), tuple(self.mds))


def _list(path: str) -> Optional[_Listing]:
    try:
        return _Listing(path)
    except OSError:
        return None


class _RootIndex:
    """The walk of one root. Answers are immutable (tuples) or fresh copies; only the validation
    bookkeeping (``dirs``, ``validated_at``) changes, under the module lock."""

    def __init__(self, root: str, generation: int) -> None:
        self.root = root
        self.generation = generation
        self.validated_at = 0
        # path -> [stat key, relevant listing (None: unreadable), racy]
        self.dirs: dict[str, list] = {}
        self.links: dict[str, Optional[tuple]] = {}
        lock_mds: list[str] = []
        index_mds: list[str] = []
        flat: dict[str, list[str]] = {}
        # (path, lock view walks it, index view walks it, ancestors' (dev, inode))
        pending: list[tuple[str, bool, bool, frozenset]] = [(root, True, True, frozenset())]
        while pending:
            path, lock_ok, index_ok, ancestors = pending.pop()
            listing = _list(path)
            if listing is None:
                self.dirs[path] = [stat_key(path), None, False]
                continue
            identity = listing.key[:2] if listing.key else None
            if identity is not None and identity in ancestors:
                continue  # a symlink loop: os.walk(followlinks=True) would re-walk it until ELOOP
            self.dirs[path] = [listing.key, listing.relevant, is_racy(listing.key, listing.read_at)]
            for name in listing.links:
                link = os.path.join(path, name)
                self.links[link] = stat_key(link)
            marker = listing.marker
            # tools.skill_lock's view: every entry named SKILL.md, symlinked dirs never entered,
            # support dirs pruned only beside a marker that exists.
            if marker is not None and lock_ok:
                lock_mds.append(os.path.join(path, "SKILL.md"))
            # iter_skill_index_files' view: SKILL.md files (not dirs), symlinked dirs entered,
            # support dirs pruned beside any SKILL.md file entry (dangling too).
            index_marker = marker is not None and not marker[0]
            if index_marker and index_ok:
                index_mds.append(os.path.join(path, "SKILL.md"))
            if lock_ok:  # legacy flat <name>.md: the rglob view (no symlinked dir entered)
                for name in listing.mds:
                    flat.setdefault(name, []).append(os.path.join(path, name))
            lock_supports = marker is not None and marker[2]
            below = ancestors | {identity} if identity is not None else ancestors
            for name, is_link in reversed(listing.subdirs):
                support = name in SKILL_SUPPORT_DIRS
                child_lock = lock_ok and not is_link and not (support and lock_supports)
                child_index = index_ok and not (support and index_marker)
                if child_lock or child_index:
                    pending.append((os.path.join(path, name), child_lock, child_index, below))
        self._lock_mds = tuple(lock_mds)
        self._index_mds = tuple(sorted(index_mds))
        self._flat = {name: tuple(sorted(paths)) for name, paths in flat.items()}
        self._lock_dirs: Optional[dict[str, tuple[str, ...]]] = None

    # ── answers ────────────────────────────────────────────────────────────────────────────────
    def skill_index_files(self) -> list[Path]:
        """``agent.skill_utils.iter_skill_index_files(root, "SKILL.md")`` as a sorted list."""
        return list(map(Path, self._index_mds))

    def flat_markdown(self, relative: str) -> list[Path]:
        """Every ``<...>/<relative>`` file the legacy flat lookup can see (``root.rglob(relative)``
        with no symlinked dir entered and no VCS/dependency/cache dir walked). *relative* is matched
        literally: ``*`` / ``?`` / ``[`` are not glob syntax here."""
        parts = Path(relative).parts
        if not parts:
            return []
        hits = self._flat.get(parts[-1], ())
        if len(parts) > 1:
            hits = tuple(p for p in hits if Path(p).parts[-len(parts):] == parts)
        return list(map(Path, hits))

    def skill_lock_dirs(self) -> dict[str, tuple[str, ...]]:
        """Directory name -> realpaths of every skill dir ``tools.skill_lock`` guards in this root
        (``is_excluded_skill_path`` applied). Built on first use, then reused."""
        found = self._lock_dirs
        if found is None:
            from agent.skill_utils import is_excluded_skill_path
            root = Path(self.root)
            names: dict[str, set[str]] = {}
            for skill_md in self._lock_mds:
                if not is_excluded_skill_path(Path(skill_md), root=root):
                    parent = os.path.dirname(skill_md)
                    names.setdefault(os.path.basename(parent), set()).add(os.path.realpath(parent))
            found = self._lock_dirs = {name: tuple(sorted(reals)) for name, reals in names.items()}
        return dict(found)

    # ── validation (module lock held) ─────────────────────────────────────────────────────────
    def still_valid(self) -> bool:
        for path, record in self.dirs.items():
            key, relevant, racy = record
            current = stat_key(path)
            if current == key and not racy:
                continue
            if current is None or key is None or relevant is None:
                return False
            listing = _list(path)
            if listing is None or listing.relevant != relevant:
                return False
            record[0] = listing.key
            record[2] = is_racy(listing.key, listing.read_at)
        return all(stat_key(link) == key for link, key in self.links.items())


def root_index(root) -> _RootIndex:
    """The current index of *root* (any path; a missing root indexes as empty)."""
    key = os.fspath(root)
    arrived = time.monotonic_ns()
    with _lock:
        index = _roots.get(key)
        if index is not None and index.generation == _generation:
            if index.validated_at > arrived:  # a check that started after we arrived answers for us
                _roots.move_to_end(key)
                return index
            started = time.monotonic_ns()
            if index.still_valid():
                index.validated_at = started
                _roots.move_to_end(key)
                return index
        started = time.monotonic_ns()
        index = _RootIndex(key, _generation)
        index.validated_at = started
        _roots[key] = index
        _roots.move_to_end(key)
        while len(_roots) > _MAX_ROOTS:
            _roots.popitem(last=False)
        return index
