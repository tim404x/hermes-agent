"""Session listings of a store that has not moved, answered without re-reading it.

The Desktop re-lists EVERY profile store on each ``sessions.changed`` (every few seconds while any bot or cron
writes anywhere) and the roster, sidebar, projects tree and status poll on top of that, each through a fresh
read-only handle. Most of those stores did not change in between: of 7 profile stores, typically 1 or 2 commit
in any given minute. Yet each listing paid the full price again: two scans of ``sessions`` with JSON
predicates, a per-row activity seek, the pinned back-fill and the local-lineage receipt scan (100 to 250 ms per
call on the big stores, much of it in Python under the GIL).

A listing is a pure function of the store's committed content and the call's arguments, so a read-only handle
keeps each answer under the store files' signature:

- ``utils.file_signature`` (mtime, size, inode, ctime) plus the device of the store file (symlinks resolved:
  SQLite writes the ``-wal`` beside the TARGET) and of its ``-wal`` when that holds frames. Every commit writes
  one of them, so a later commit moves the signature; ctime also catches a restore that puts an older mtime
  back (``cp -p``, ``rsync -t``), since userland cannot backdate it. The ``-wal`` is stat'ed before the store
  file, so a checkpoint that deletes it in between leaves a racy store file, never a pre-commit signature;
- **no signature while either file's mtime or ctime is younger than** ``utils.RACY_MTIME_WINDOW_NS`` (2 s): a
  second commit inside the same timestamp tick, rewriting the WAL in place at the same size, could otherwise
  hide behind the first one. A store that committed in the last 2 s is simply read again, exactly as before;
- the signature is taken BEFORE the read, so a kept answer is never older than the signature it is kept under;
- **an answer kept while the WAL held frames expires after** ``_MAX_AGE_S``: a WAL commit is written first
  and published in ``-shm`` only after its fsync (``synchronous=FULL``), which moves no stat. A writer stalled
  past the racy window there lets a read see the WAL settled but the commit not yet visible; the age cap bounds
  that (rare) case to seconds, not until the next commit. With no WAL frames nothing can be pending, so those
  answers live until the store moves.

Only read-only holders (the browse handles) use it: a writer's own uncommitted or just-committed rows are its
own business, reads inside ``transcript_read_snapshot`` or an open transaction see their own generation, and a
holder still reading a store file that has since been replaced at its path (or whose file identity is unknown)
reads that file, never the memo's. Only compact listings are kept (full rows carry ~50 KB of system prompt
each); the Desktop's are all compact.

Kept answers are shared, so they are immutable: frozen listing entries whose ``model_config`` / ``origin``
mappings refuse mutation, ints, and the lineage projection is copied out per caller. Every compact listing a
read-only handle returns has those immutable mappings, kept or not, so a consumer that mutates one fails in its
first test rather than only in production. ``HERMES_LISTING_MEMO=0`` turns every lookup into a plain read.
"""

from __future__ import annotations

import dataclasses
import os
import stat
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional, TypeVar

from utils import RACY_MTIME_WINDOW_NS, file_signature

T = TypeVar("T")

_MAX_ENTRIES = 512
_MAX_STORE_PATHS = 256
# Kept listing rows across every answer (a compact row is one frozen entry, a few KB): bounds memory when a
# client pages deep (``limit + offset`` windows) on many stores. A Desktop working set is ~3-4k rows.
_MAX_ROWS = 10_000
# Seconds a kept answer is trusted at most (see the module docstring: a commit published after its WAL stat
# settled). The Desktop re-asks every 2.5-10 s, so a quiet store is still read at most once per window.
_MAX_AGE_S = 15.0
_DISABLE_ENV = "HERMES_LISTING_MEMO"

# (store file, kind, call key) -> (signature, answer, weight, kept at monotonic seconds)
_memo: OrderedDict[tuple, tuple[tuple, Any, int, float]] = OrderedDict()
_weight = [0]
_lock = threading.Lock()
_store_paths: OrderedDict[str, tuple[tuple[int, int], str]] = OrderedDict()
_store_paths_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _now_ns() -> int:
    return time.time_ns()


def _file_parts(path: str, now_ns: int, *, wal: bool) -> Optional[tuple]:
    """``(dev, *file_signature)``; ``()`` for an absent or frameless WAL; None when racy or unreadable."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return () if wal else None
    except OSError:
        return None
    if wal and st.st_size == 0:
        return ()  # a read-only open's empty sidecar carries no frames: not a change
    if now_ns - max(st.st_mtime_ns, st.st_ctime_ns) < RACY_MTIME_WINDOW_NS:
        return None  # a same-tick, same-size rewrite after this stat could not move it
    return (st.st_dev, *file_signature(st))


def _resolved_store_path(db_path) -> str:
    """Reuse a store's spelling across browse handles only while it names the same file."""
    path = os.fspath(db_path)
    try:
        info = os.stat(path)
    except OSError:
        with _store_paths_lock:
            _store_paths.pop(path, None)
        return os.path.realpath(path)
    identity = (info.st_dev, info.st_ino)
    with _store_paths_lock:
        cached = _store_paths.get(path)
        if cached is not None and identity == cached[0]:
            try:
                resolved_info = os.lstat(cached[1])
            except OSError:
                resolved_info = None
            if (
                resolved_info is not None
                and stat.S_ISREG(resolved_info.st_mode)
                and (resolved_info.st_dev, resolved_info.st_ino) == identity
            ):
                _store_paths.move_to_end(path)
                return cached[1]
        resolved = os.path.realpath(path)
        try:
            info = os.stat(resolved)
        except OSError:
            _store_paths.pop(path, None)
            return resolved
        _store_paths[path] = ((info.st_dev, info.st_ino), resolved)
        if len(_store_paths) > _MAX_STORE_PATHS:
            _store_paths.popitem(last=False)
        return resolved


def store_signature(db_path) -> Optional[tuple]:
    """The committed-content signature of the store at *db_path*, or None when it cannot vouch for one."""
    if db_path is None:
        return None
    return _signature(_resolved_store_path(db_path))  # SQLite puts the -wal beside the symlink's target


def _signature(path: str) -> Optional[tuple]:
    """``store_signature`` of an already resolved store path: ``((dev, ino), db parts, wal parts)``."""
    now_ns = _now_ns()
    wal = _file_parts(f"{path}-wal", now_ns, wal=True)  # before the store file: see the module docstring
    if wal is None:
        return None
    db = _file_parts(path, now_ns, wal=False)
    if not db:
        return None
    dev, _mtime, _size, ino = db[:4]  # (st_dev, *utils.file_signature): mtime, size, ino, ctime
    return (dev, ino), db, wal


def _memo_store(holder) -> Optional[str]:
    """The store a read through *holder* may be answered from the memo for, or None (a plain read)."""
    if not enabled() or not getattr(holder, "read_only", False):
        return None
    path = getattr(holder, "db_path", None)
    if path is None:
        return None
    pinned = getattr(holder, "_pinned_read_conn", None)
    if pinned is not None and pinned() is not None:
        return None  # inside transcript_read_snapshot: that generation, not the latest
    conn = getattr(holder, "_conn", None)
    if conn is not None and getattr(conn, "in_transaction", False):
        return None
    from persistence.sqlite._state.health import STORAGE_OK, storage_state

    if storage_state(path) != STORAGE_OK:
        return None  # latched corrupt: every listing reads (and fails) as every other read of it does
    return _resolved_store_path(path)


def _reads_this_file(holder, signature: tuple) -> bool:
    """Whether *holder* provably reads the file now at its path: a replaced store keeps old handles on the old
    file, and a holder whose file identity is unknown cannot tell."""
    opened = getattr(holder, "_db_file_identity", None)
    return opened is not None and tuple(opened) == signature[0]


def _visible_signature(holder, store: str) -> Optional[tuple]:
    """``((dev, ino), ("rev", revision, schema cookie), ())`` from the store's visible list revision
    (``list_revision``), or None when the store cannot vouch for one (no triggers yet, a read error): the caller
    then uses the files' stat signature. Exact for a listing that cannot show internal rows, and it holds through
    the internal-row writes that move the store's files every second on a busy store (and its checkpoints, which
    move the store file itself every few seconds there: no file stat can stay in this key).

    Every write that goes through SQLite runs the triggers, whoever writes; a rename over the path moves (dev, ino)
    (``_reads_this_file``) and a ``backup()`` restore or any DDL moves the schema cookie. A rewrite that bypasses
    SQLite in place (a damaged page) is caught by the corruption latch the next real read trips (``_memo_store``).
    No age cap: the counter is read in its own read transaction BEFORE the listing's read, so a commit published
    late (the stat signature's ``_MAX_AGE_S`` case) is either in neither read or moves the counter for every read
    after the first that sees it."""
    from persistence.sqlite.conversation._store.list_revision import visible_list_revision

    try:
        st = os.stat(store)
        with holder._read_ctx() as conn:
            revision = visible_list_revision(conn)
    except Exception:  # health: allow BLE001 -- any failed read means "no revision": the stat signature decides
        return None
    if revision is None:
        return None
    return (st.st_dev, st.st_ino), ("rev", *revision), ()


def _fresh(kept: tuple, signature: tuple, now: float) -> bool:
    if kept[0] != signature:
        return False
    # Frames in the WAL: one of them could have been published after the read (see the module docstring).
    return signature[2] == () or now - kept[3] < _MAX_AGE_S


# (store, "rev" | "stat", "hit" | "miss") -> count: what a bench or a debugging session reads (``stats()``).
_counts: dict[tuple, int] = {}


def _count(store: str, family: str, outcome: str) -> None:
    key = (store, family, outcome)
    if key in _counts or len(_counts) < 4 * _MAX_ENTRIES:
        _counts[key] = _counts.get(key, 0) + 1


def stats() -> dict[tuple, int]:
    """Hits and misses per store and signature kind since the process started (or ``clear()``)."""
    with _lock:
        return dict(_counts)


def _family(signature: tuple) -> str:
    return "rev" if signature[1][:1] == ("rev",) else "stat"


def memoized(holder, kind: str, key: tuple, compute: Callable[[], T],
             keep: Optional[Callable[[T], T]] = None, weigh: Optional[Callable[[T], int]] = None, *,
             visible_only: bool = False) -> T:
    """``compute()``, or the answer it gave for *key* on this store while the store has not moved since.
    ``keep`` makes an answer safe to share: every read-only caller gets its result, kept or not. ``weigh``
    counts an answer's rows. ``visible_only``: the answer can neither include nor depend on an internal row
    (``list_revision``), so the store's visible list revision keys it when the store has one; the files' stat
    signature otherwise."""
    store = _memo_store(holder)
    if store is None:
        return compute()
    # BEFORE the read: the answer is at least this new.
    signature = (_visible_signature(holder, store) if visible_only else None) or _signature(store)
    if signature is None or not _reads_this_file(holder, signature):
        answer = compute()
        return keep(answer) if keep is not None else answer
    slot = (store, kind, key)
    family = _family(signature)
    now = time.monotonic()
    with _lock:
        hit = _memo.get(slot)
        if hit is not None and _fresh(hit, signature, now):
            _memo.move_to_end(slot)
            _count(store, family, "hit")
            return hit[1]
        _count(store, family, "miss")
    answer = compute()
    if keep is not None:
        answer = keep(answer)
    weight = (weigh(answer) if weigh is not None else 1) + 1
    if weight > _MAX_ROWS:
        return answer
    with _lock:
        # A moved store obsoletes every answer kept under its older signatures of the same kind: a revision-keyed
        # answer and a stat-keyed one are kept side by side (a quiet revision outlives every stat move).
        for k in [k for k, kept in _memo.items()
                  if k[0] == store and kept[0] != signature and _family(kept[0]) == family] + [slot]:
            dropped = _memo.pop(k, None)
            if dropped is not None:
                _weight[0] -= dropped[2]
        _memo[slot] = (signature, answer, weight, now)
        _weight[0] += weight
        while len(_memo) > _MAX_ENTRIES or _weight[0] > _MAX_ROWS:
            _k, dropped = _memo.popitem(last=False)
            _weight[0] -= dropped[2]
    return answer


def frozen_key(*parts: Any) -> tuple:
    """A hashable key of call arguments (lists and sets become tuples, mappings sorted item tuples)."""
    def freeze(value):
        if isinstance(value, (list, tuple)):
            return tuple(freeze(v) for v in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted(freeze(v) for v in value))
        if isinstance(value, dict):
            return tuple(sorted((k, freeze(v)) for k, v in value.items()))
        return value
    return tuple(freeze(p) for p in parts)


def _refuse(self, *args, **kwargs):
    raise TypeError("a listing entry is shared by every caller of the listing memo: copy it before mutating")


class SharedMapping(dict):
    """A ``dict`` that refuses mutation: reads, ``isinstance(x, dict)`` and JSON encoding work as before.
    ``copy.deepcopy`` and pickling give plain, mutable containers all the way down; ``dict(x)``, ``x.copy()``,
    ``copy.copy`` and ``x | y`` are shallow: the top level is a plain dict, nested values stay shared."""

    __slots__ = ()
    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        import copy

        return {copy.deepcopy(k, memo): copy.deepcopy(v, memo) for k, v in self.items()}

    def __reduce__(self):
        return dict, (dict(self),)


class SharedList(list):
    """A ``list`` that refuses mutation (see :class:`SharedMapping`)."""

    __slots__ = ()
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _refuse
    append = clear = extend = insert = pop = remove = reverse = sort = _refuse

    def __copy__(self):
        return list(self)

    def __deepcopy__(self, memo):
        import copy

        return [copy.deepcopy(v, memo) for v in self]

    def __reduce__(self):
        return list, (list(self),)


def _shared(value):
    if isinstance(value, dict):
        return SharedMapping((k, _shared(v)) for k, v in value.items())
    if isinstance(value, list):
        return SharedList(_shared(v) for v in value)
    return value


def shared_entries(entries: tuple) -> tuple:
    """Freshly read listing entries made safe to hand to every caller: their sessions' ``model_config`` and
    ``origin`` refuse mutation. In place: nobody holds these entries yet, and copying a 76-field frozen record
    per row cost the miss path ~40 us a row."""
    for entry in entries:
        session = getattr(entry, "session", None)
        if session is None or isinstance(session, type) or not dataclasses.is_dataclass(session):
            continue
        for name in ("model_config", "origin"):
            value = getattr(session, name, None)
            if isinstance(value, (dict, list)) and not isinstance(value, (SharedMapping, SharedList)):
                object.__setattr__(session, name, _shared(value))  # a frozen dataclass: set past its guard
    return entries


def clear() -> None:
    """Drop every kept answer (tests; a caller that knows better)."""
    with _lock:
        _memo.clear()
        _weight[0] = 0
        _counts.clear()
    with _store_paths_lock:
        _store_paths.clear()
