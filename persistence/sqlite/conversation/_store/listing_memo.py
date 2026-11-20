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
- **every kept answer expires after** ``_MAX_AGE_S``: a WAL commit is written first and published in ``-shm``
  only after its fsync, which moves no stat. A fsync stalled past the racy window lets a read see the WAL
  settled but the commit not yet visible; the age cap bounds that (rare) case to seconds, not until the next
  commit.

Only read-only holders (the browse handles) use it: a writer's own uncommitted or just-committed rows are its
own business, reads inside ``transcript_read_snapshot`` or an open transaction see their own generation, and a
holder still reading a store file that has since been replaced at its path reads that file, never the memo's.
Only compact listings are kept (full rows carry ~50 KB of system prompt each); the Desktop's are all compact.

Kept answers are shared, so they are immutable: frozen listing entries whose ``model_config`` / ``origin``
mappings refuse mutation (copy them first), ints, and the lineage projection is copied out per caller.
``HERMES_LISTING_MEMO=0`` turns every lookup into a plain read.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional, TypeVar

from utils import RACY_MTIME_WINDOW_NS, file_signature

T = TypeVar("T")

_MAX_ENTRIES = 512
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


def store_signature(db_path) -> Optional[tuple]:
    """The committed-content signature of the store at *db_path*, or None when it cannot vouch for one."""
    if db_path is None:
        return None
    path = os.path.realpath(db_path)  # SQLite puts the -wal beside the symlink's target
    now_ns = _now_ns()
    wal = _file_parts(f"{path}-wal", now_ns, wal=True)  # before the store file: see the module docstring
    if wal is None:
        return None
    db = _file_parts(path, now_ns, wal=False)
    if not db:
        return None
    return db, wal


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
    return os.path.realpath(path)


def _reads_this_file(holder, signature: tuple) -> bool:
    """Whether *holder* still reads the file now at its path (a replaced store keeps old handles on the old one).
    ``signature[0]`` starts with the store file's ``(st_dev, st_mtime_ns, st_size, st_ino, ...)``."""
    opened = getattr(holder, "_db_file_identity", None)
    if opened is None:
        return True
    dev, _mtime, _size, ino = signature[0][:4]
    return tuple(opened) == (dev, ino)


def memoized(holder, kind: str, key: tuple, compute: Callable[[], T],
             keep: Optional[Callable[[T], T]] = None, weigh: Optional[Callable[[T], int]] = None) -> T:
    """``compute()``, or the answer it gave for *key* on this store while the store has not moved since.
    ``keep`` makes an answer safe to share (it is what every caller gets); ``weigh`` counts its rows."""
    store = _memo_store(holder)
    if store is None:
        return compute()
    signature = store_signature(store)  # BEFORE the read: the answer is at least this new
    if signature is None or not _reads_this_file(holder, signature):
        return compute()
    slot = (store, kind, key)
    now = time.monotonic()
    with _lock:
        hit = _memo.get(slot)
        if hit is not None and hit[0] == signature and now - hit[3] < _MAX_AGE_S:
            _memo.move_to_end(slot)
            return hit[1]
    answer = compute()
    if keep is not None:
        answer = keep(answer)
    weight = (weigh(answer) if weigh is not None else 1) + 1
    if weight > _MAX_ROWS:
        return answer
    with _lock:
        # A moved store obsoletes every answer kept under its older signatures.
        for k in [k for k, kept in _memo.items() if k[0] == store and kept[0] != signature] + [slot]:
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
    """A ``dict`` that refuses mutation: reads, ``isinstance(x, dict)`` and JSON encoding work as before;
    ``dict(x)`` / ``copy`` / ``deepcopy`` / pickling give a plain, mutable copy."""

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
    """Listing entries made safe to hand to every caller: the session's mappings refuse mutation."""
    out = []
    for entry in entries:
        session = getattr(entry, "session", None)
        if session is None or isinstance(session, type) or not dataclasses.is_dataclass(session):
            out.append(entry)
            continue
        changes = {name: _shared(getattr(session, name)) for name in ("model_config", "origin")
                   if isinstance(getattr(session, name, None), (dict, list))}
        out.append(dataclasses.replace(entry, session=dataclasses.replace(session, **changes)) if changes else entry)
    return tuple(out)


def clear() -> None:
    """Drop every kept answer (tests; a caller that knows better)."""
    with _lock:
        _memo.clear()
        _weight[0] = 0
