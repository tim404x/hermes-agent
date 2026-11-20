"""Session listings of a store that has not moved, answered without re-reading it.

The Desktop re-lists EVERY profile store on each ``sessions.changed`` (every few seconds while any bot or cron
writes anywhere) and the roster, sidebar, projects tree and status poll on top of that, each through a fresh
read-only handle. Most of those stores did not change in between: of 7 profile stores, typically 1 or 2 commit
in any given minute. Yet each listing paid the full price again: two scans of ``sessions`` with JSON
predicates, a per-row activity seek, the pinned back-fill and the local-lineage receipt scan (100 to 250 ms per
call on the big stores, much of it in Python under the GIL).

A listing is a pure function of the store's committed content and the call's arguments, so a read-only handle
keeps each answer under the store files' signature:

- ``utils.file_signature`` (mtime, size, inode, ctime) plus the device of ``state.db`` and of its ``-wal``
  when that holds frames. Every commit writes one of them, so a later commit moves the signature; ctime also
  catches a restore that puts an older mtime back (``cp -p``, ``rsync -t``), since userland cannot backdate it;
- **no signature while either file's mtime or ctime is younger than** ``utils.RACY_MTIME_WINDOW_NS`` (2 s): a
  second commit inside the same timestamp tick, rewriting the WAL in place at the same size, could otherwise
  hide behind the first one. A store that committed in the last 2 s is simply read again, exactly as before;
- the signature is taken BEFORE the read, so a kept answer is never older than the signature it is kept under.

The same signature ``profile_roster_cache.store_signature`` keys the roster's session fields on. Only read-only
holders (the browse handles) use it: a writer's own uncommitted or just-committed rows are its own business, and
reads inside ``transcript_read_snapshot`` or an open transaction see their own generation, never the memo's.

Kept answers are immutable (frozen listing entries, ints, the lineage projection is copied out), so a hit hands
the same objects to every caller. ``HERMES_LISTING_MEMO=0`` turns every lookup into a plain read.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from utils import RACY_MTIME_WINDOW_NS, file_signature

T = TypeVar("T")

_MAX_ENTRIES = 512
# Kept listing rows across every answer (a row is one frozen entry, a few KB): bounds memory when a client
# pages deep (``limit + offset`` windows) on many stores. A Desktop working set is ~3-4k rows.
_MAX_ROWS = 10_000
_DISABLE_ENV = "HERMES_LISTING_MEMO"

# (resolved db path, kind, call key) -> (signature, answer, weight)
_memo: OrderedDict[tuple, tuple[tuple, Any, int]] = OrderedDict()
_weight = [0]
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get(_DISABLE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _now_ns() -> int:
    return time.time_ns()


def _file_parts(path: Path, now_ns: int) -> Optional[tuple]:
    """``(dev, *file_signature)``; ``()`` for an absent or frameless WAL; None when racy or unreadable."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return () if path.name.endswith("-wal") else None
    except OSError:
        return None
    if path.name.endswith("-wal") and st.st_size == 0:
        return ()  # a read-only open's empty sidecar carries no frames: not a change
    if now_ns - max(st.st_mtime_ns, st.st_ctime_ns) < RACY_MTIME_WINDOW_NS:
        return None  # a same-tick, same-size rewrite after this stat could not move it
    return (st.st_dev, *file_signature(st))


def store_signature(db_path) -> Optional[tuple]:
    """The committed-content signature of the store at *db_path*, or None when it cannot vouch for one."""
    if db_path is None:
        return None
    path = Path(db_path)
    now_ns = _now_ns()
    db = _file_parts(path, now_ns)
    if not db:
        return None
    wal = _file_parts(Path(f"{path}-wal"), now_ns)
    if wal is None:
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
    return str(path)


def memoized(holder, kind: str, key: tuple, compute: Callable[[], T]) -> T:
    """``compute()``, or the answer it gave for *key* on this store while the store has not moved since."""
    store = _memo_store(holder)
    if store is None:
        return compute()
    signature = store_signature(store)  # BEFORE the read: the answer is at least this new
    if signature is None:
        return compute()
    slot = (store, kind, key)
    with _lock:
        hit = _memo.get(slot)
        if hit is not None and hit[0] == signature:
            _memo.move_to_end(slot)
            return hit[1]
    answer = compute()
    weight = len(answer) + 1 if isinstance(answer, tuple) else 1
    if weight > _MAX_ROWS:
        return answer
    with _lock:
        # A moved store obsoletes every answer kept under its older signatures.
        for k in [k for k, kept in _memo.items() if k[0] == store and kept[0] != signature] + [slot]:
            dropped = _memo.pop(k, None)
            if dropped is not None:
                _weight[0] -= dropped[2]
        _memo[slot] = (signature, answer, weight)
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


def clear() -> None:
    """Drop every kept answer (tests; a caller that knows better)."""
    with _lock:
        _memo.clear()
        _weight[0] = 0
