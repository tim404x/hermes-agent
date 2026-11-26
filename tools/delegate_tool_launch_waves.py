"""Launch waves and the child-start boundary for subagents.

``delegation.launch_wave_size`` / ``delegation.launch_wave_seconds``: a provider rate-limits sudden ramps, not
only steady load: 80 children whose first requests (each a fresh prompt-cache write) land in the same second
draw 429s that a gradual start would not. With a wave size N, at most N subagents START in any
``launch_wave_seconds`` window, and the rest wait for their wave in launch order. Only starts are paced: a
running child is never throttled. Size 0 (the default) turns it off.

Each profile and provider account keeps its own schedule (one process serves many profiles, and the ramp limit
belongs to the account), admission is decided under the lock from ACTUAL recent starts (a waiter that wakes late
cannot join an earlier wave), and a waiter is only a place in the queue: one that is stopped leaves it and never
holds a window.

``run_child_start`` is the one boundary every child start passes, batch (``delegate_tool_dispatch``) and the
public lifecycle API (``delegate_tool_results._run_child_lifecycle``) alike: the parent's live fan-out cap
(``delegation.max_children_per_child`` for a subagent parent), then a slot of this process's in-process child
cap (``delegation.max_in_process_children``, tools/delegate_tool_process_slots.py), then the launch wave, exactly
once per child.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

from hermes_constants import get_hermes_home
from tools.delegate_tool_config import _get_in_process_children_limits, _get_launch_waves, _get_max_children_per_child
from tools.delegate_tool_process_slots import (
    admit_in_process_child, in_process_children_running, release_in_process_child,
)

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

_POLL_S = 0.05
_T = TypeVar("_T")


class LaunchPacer:
    """One sliding-window schedule: at most ``size`` actual starts in any ``window_s``, waiters admitted in
    launch order. Nothing is reserved ahead: a stopped waiter leaves the queue with its window still free."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock, self._cond = clock, threading.Condition()
        self._starts: deque[float] = deque()  # actual admission times inside the current window, oldest first
        self._queue: deque[object] = deque()  # waiting launches, in launch order
        self._admitted = 0

    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    def started(self) -> int:
        with self._cond:
            return self._admitted

    def _opens_at(self, size: int, window_s: float, now: float) -> float:
        while self._starts and self._starts[0] <= now - window_s:
            self._starts.popleft()
        return now if len(self._starts) < size else self._starts[-size] + window_s

    def wait_turn(self, size: int, window_s: float, stopped: Callable[[], bool]) -> bool:
        """Block until this launch is admitted (True) or ``stopped()`` turns true first (False)."""
        ticket, held = object(), False
        with self._cond:
            self._queue.append(ticket)
            try:
                while not stopped():
                    now = self._clock()
                    timeout = _POLL_S
                    if self._queue[0] is ticket:
                        opens_at = self._opens_at(size, window_s, now)
                        if opens_at <= now:
                            self._starts.append(now)
                            self._admitted += 1
                            return True
                        timeout = min(_POLL_S, opens_at - now)
                    if not held:
                        held = True
                        logger.info("delegate: subagent launch held for its wave (launch_wave_size=%d, %d waiting)",
                                    size, len(self._queue))
                    self._cond.wait(timeout)
                return False
            finally:
                self._queue.remove(ticket)
                self._cond.notify_all()


class LaunchSchedules:
    """One :class:`LaunchPacer` per (profile home, provider account): profiles and accounts never share a ramp."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock, self._lock, self._pacers = clock, threading.Lock(), {}

    def pacer(self, key: tuple) -> LaunchPacer:
        with self._lock:
            if key not in self._pacers:
                self._pacers[key] = LaunchPacer(self._clock)
            return self._pacers[key]

    def waiting(self) -> int:
        with self._lock:
            pacers = list(self._pacers.values())
        return sum(p.waiting() for p in pacers)

    def started(self) -> int:
        with self._lock:
            pacers = list(self._pacers.values())
        return sum(p.started() for p in pacers)


_SCHEDULES = LaunchSchedules()


def _account_key(child: Any) -> tuple[str, str, str]:
    """The provider rate-limit identity a child starts on: provider, endpoint, and its credential pool's name
    (``custom:account-a``; named providers at one gateway URL each own a pool) or its key (hashed, never kept
    in clear). The pool's name, never the object: a rebuilt view of the same pool is the same account."""
    provider = getattr(child, "provider", "")
    base_url = getattr(child, "base_url", "")
    provider = provider if isinstance(provider, str) else ""
    base_url = base_url.rstrip("/").lower() if isinstance(base_url, str) else ""
    pool = getattr(child, "_credential_pool", None)
    if pool is not None:
        name = getattr(pool, "provider", "")
        return provider, base_url, f"pool:{name if isinstance(name, str) else ''}"
    api_key = getattr(child, "api_key", None)
    key = hashlib.sha256(api_key.encode()).hexdigest()[:16] if isinstance(api_key, str) and api_key else ""
    return provider, base_url, key


def _stopped(agent: Any) -> bool:
    return getattr(agent, "_interrupt_requested", False) is True


def wait_for_launch_wave(child: Any, parent: Optional[Any] = None) -> bool:
    """True when ``child`` may start (waves off, or its wave opened); False when it, or ``parent`` when given,
    was stopped while it waited (the caller releases it unstarted). The schedule is the calling profile's own
    for the child's provider account, sized by that profile's ``launch_wave_size``."""
    size, window_s = _get_launch_waves()
    if size <= 0 or window_s <= 0:
        return True
    pacer = _SCHEDULES.pacer((str(get_hermes_home()), *_account_key(child)))
    return pacer.wait_turn(size, window_s, lambda: _stopped(child) or (parent is not None and _stopped(parent)))


@dataclass(frozen=True)
class StartRefusal:
    """Why a built child must not start: ``status`` is its result entry's status."""

    status: str
    reason: str


_LIVE_LOCK = threading.Lock()


def _subagent_depth(parent: Any) -> int:
    depth = getattr(parent, "_delegate_depth", 0)
    return depth if isinstance(depth, int) and not isinstance(depth, bool) else 0


def _claim_live_slot(parent: Any) -> tuple[bool, Optional[StartRefusal]]:
    """``(claimed, refusal)``: a subagent parent (depth >= 1) holds one slot per running child, at most
    ``delegation.max_children_per_child`` of them. A top-level parent is not counted here."""
    if parent is None or _subagent_depth(parent) < 1:
        return False, None
    cap = _get_max_children_per_child()
    with _LIVE_LOCK:
        live = getattr(parent, "_delegate_live_children", 0)
        live = live if isinstance(live, int) and not isinstance(live, bool) else 0
        if cap and live >= cap:
            return False, StartRefusal("failed", (
                f"Not started: this subagent already runs {live} child(ren) and delegation.max_children_per_child "
                f"is {cap}. Wait for one to finish, or do the work yourself."))
        parent._delegate_live_children = live + 1
    return True, None


def _release_live_slot(parent: Any) -> None:
    with _LIVE_LOCK:
        live = getattr(parent, "_delegate_live_children", 0)
        parent._delegate_live_children = max(0, live - 1) if isinstance(live, int) else 0


def _claim_process_slot(child: Any, parent: Optional[Any]) -> tuple[bool, Optional[StartRefusal]]:
    """``(holds_slot, refusal)``: wait for one of this process's in-process child slots
    (``delegation.max_in_process_children``, tools/delegate_tool_process_slots.py)."""
    admitted = admit_in_process_child(child, parent)
    if admitted:
        return True, None
    if admitted is False:
        return False, StartRefusal("interrupted", "Stopped while waiting for an in-process subagent slot")
    cap, wait_s = _get_in_process_children_limits()
    return False, StartRefusal("failed", (
        f"Not started: this Hermes process already runs {in_process_children_running()} subagent(s) "
        f"(delegation.max_in_process_children={cap}) and none finished within {wait_s:g}s. Retry later with fewer "
        f"parallel tasks, or do the work yourself."))


def run_child_start(child: Any, parent: Any, run: Callable[[], _T], refuse: Callable[[StartRefusal], _T], *,
                    honor_parent_interrupt: bool = True) -> _T:
    """Start ``child`` through the common boundary: its parent's live fan-out cap, then an in-process child slot,
    then its launch wave. Admitted: ``run()`` (both slots are released when it returns). Refused or stopped while waiting: ``refuse(refusal)``, which
    must release the unstarted child. Exactly once per child: an already admitted child just runs. A detached
    (background or lifecycle) start ignores the parent's turn interrupt; only the child's own stop ends its wait."""
    if getattr(child, "_delegate_start_admitted", False) is True:
        return run()
    claimed, refusal = _claim_live_slot(parent)
    slot = False
    if refusal is None:
        slot, refusal = _claim_process_slot(child, parent if honor_parent_interrupt else None)
    if refusal is None and not wait_for_launch_wave(child, parent if honor_parent_interrupt else None):
        refusal = StartRefusal("interrupted", "Stopped while waiting for its launch wave")
    if refusal is not None:
        if slot:
            release_in_process_child(child)
        if claimed:
            _release_live_slot(parent)
        return refuse(refusal)
    child._delegate_start_admitted = True
    try:
        return run()
    finally:
        if slot:
            release_in_process_child(child)
        if claimed:
            _release_live_slot(parent)
