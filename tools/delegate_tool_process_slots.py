"""Per-process cap on RUNNING in-process subagents: ``delegation.max_in_process_children``.

Every ``delegate_task`` child runs as threads of the process that runs its parent's turn. In the gateway that is
the one process that also runs every profile's CLI/one-shot/cron/messaging turn and the owner event loop, all on
one GIL. On 24 Nov 2026 a Telegram orchestration chat's ~26 in-process children (depth 1 and 2) against ~8
top-level turns starved that loop for ~90 s until the liveness watchdog exited the gateway (lane report
``~/.hermes/work/gateway-loop-stall/lane-subagents.md``). ``max_concurrent_children`` cannot bound this: it is per
call, and nested and concurrent calls multiply it.

So at most N children RUN in this process at once, whatever profile, parent or start path launched them
(``run_child_start`` is the one boundary every start passes). Over the cap a child QUEUES (launch order, stop-aware)
for at most ``delegation.in_process_wait_seconds``, then is refused with a reason the parent's model can act on.
It does not run in another process: no out-of-process path keeps the child result contract (live progress relay,
parent stop fan-out and steer, credential-pool leases, tree usage roll-up, ``output_schema``, transcripts). The
managed worker runs whole owner-admitted session turns and returns a turn frame, the dashboard compute host owns
TUI sessions, and ``hermes chat --oneshot`` executes its turn back inside the gateway.

A parent blocked in a synchronous ``delegate_task`` join is not running: it LENDS its slot to its children for
the join and takes one back, ahead of new launches, when they return. A subagent's delegations are always
synchronous, so without the lend depth-1 orchestrators holding every slot while their depth-2 children queue
would deadlock the pool until the wait bound refused the children.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

from tools.delegate_tool_config import _get_in_process_children_limits

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

_POLL_S = 0.05
# Per-agent slot state, read and written only under the pool's condition lock.
_HELD, _DONE = "_delegate_process_slot", "_delegate_process_slot_done"
_LENDS, _LENT = "_delegate_process_slot_lends", "_delegate_process_slot_lent"
_WAITING = "_delegate_process_slot_waiting"


def _flag(agent: Any, name: str) -> bool:
    return getattr(agent, name, False) is True


def _lends(agent: Any) -> int:
    value = getattr(agent, _LENDS, 0)
    return value if type(value) is int else 0


class InProcessChildSlots:
    """The children running in this process, the launches waiting for one of its slots (launch order) and the
    parents taking back a lent slot (served before any launch). Every admitted child is counted, capped or not,
    so a cap switched on at runtime sees the real load."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock, self._cond = clock, threading.Condition()
        self._running = 0
        self._peak = 0
        self._launches: deque[object] = deque()
        self._resumes: deque[object] = deque()

    def running(self) -> int:
        with self._cond:
            return self._running

    def peak(self) -> int:
        with self._cond:
            return self._peak

    def waiting(self) -> int:
        with self._cond:
            return len(self._launches) + len(self._resumes)

    def _take(self) -> None:
        self._running += 1
        self._peak = max(self._peak, self._running)

    def _free(self, cap: int) -> bool:
        return cap <= 0 or self._running < cap

    def admit(self, agent: Any, cap: int, wait_s: float, stopped: Callable[[], bool]) -> Optional[bool]:
        """Admit ``agent`` as a running child: True (it holds a slot), False (stopped while waiting) or None (no
        slot freed within ``wait_s`` > 0)."""
        ticket, held, start = object(), False, self._clock()
        with self._cond:
            self._launches.append(ticket)
            try:
                while True:
                    # A free slot admits at once, stop or not (the run's own stop handling applies, as without a
                    # cap); only a launch that has to WAIT leaves the queue when it or its parent is stopped.
                    if self._launches[0] is ticket and not self._resumes and self._free(cap):
                        self._take()
                        setattr(agent, _HELD, True)
                        return True
                    if stopped():
                        return False
                    now = self._clock()
                    if wait_s > 0 and now - start >= wait_s:
                        return None
                    # Ticks in a background unit's progress token: queued for a slot is not stalled.
                    setattr(agent, _WAITING, int(now))
                    if not held:
                        held = True
                        logger.info("delegate: subagent start held: %d running in this process "
                                    "(delegation.max_in_process_children=%d), %d waiting",
                                    self._running, cap, len(self._launches))
                    self._cond.wait(_POLL_S)
            finally:
                self._launches.remove(ticket)
                if getattr(agent, _WAITING, None) is not None:
                    setattr(agent, _WAITING, None)
                self._cond.notify_all()

    def release(self, agent: Any) -> None:
        """``agent``'s run ended: free its slot if it holds one; a lend still open never takes one back."""
        with self._cond:
            setattr(agent, _DONE, True)
            if _flag(agent, _HELD):
                setattr(agent, _HELD, False)
                self._running = max(0, self._running - 1)
                self._cond.notify_all()

    def lend(self, agent: Any) -> None:
        """``agent`` blocks on its own children: the first open lend frees its slot (if it holds one) for them."""
        with self._cond:
            lends = _lends(agent)
            setattr(agent, _LENDS, lends + 1)
            if lends == 0 and _flag(agent, _HELD):
                setattr(agent, _HELD, False)
                setattr(agent, _LENT, True)
                self._running = max(0, self._running - 1)
                self._cond.notify_all()

    def take_back(self, agent: Any, cap: int, stopped: Callable[[], bool]) -> None:
        """End one lend; the last one takes the lent slot back, before any waiting launch. A stopped agent takes it
        at once (over the cap only while it unwinds) so its release stays balanced; an agent whose run already
        ended (abandoned by its parent) takes nothing."""
        ticket = object()
        with self._cond:
            lends = max(0, _lends(agent) - 1)
            setattr(agent, _LENDS, lends)
            if lends or not _flag(agent, _LENT):
                return
            setattr(agent, _LENT, False)
            self._resumes.append(ticket)
            try:
                while not _flag(agent, _DONE):
                    if stopped() or (self._resumes[0] is ticket and self._free(cap)):
                        self._take()
                        setattr(agent, _HELD, True)
                        return
                    self._cond.wait(_POLL_S)
            finally:
                self._resumes.remove(ticket)
                self._cond.notify_all()


_SLOTS = InProcessChildSlots()


def _stopped(agent: Any) -> bool:
    return getattr(agent, "_interrupt_requested", False) is True


def admit_in_process_child(child: Any, parent: Optional[Any] = None) -> Optional[bool]:
    """Wait for one of this process's child slots: True when ``child`` may run (release it with
    :func:`release_in_process_child`), False when it or ``parent`` (when given) was stopped while waiting, None
    when no slot freed within ``delegation.in_process_wait_seconds``."""
    cap, wait_s = _get_in_process_children_limits()
    return _SLOTS.admit(child, cap, wait_s, lambda: _stopped(child) or (parent is not None and _stopped(parent)))


def release_in_process_child(child: Any) -> None:
    _SLOTS.release(child)


@contextmanager
def lend_in_process_child_slot(agent: Any) -> Iterator[None]:
    """For a synchronous join: ``agent``, when it is an in-process child holding a slot, lends that slot to the
    children it waits on and takes one back, ahead of new launches, when the join returns (nested joins lend once).
    For any other agent (a top-level parent) this is a no-op."""
    if agent is None:
        yield
        return
    _SLOTS.lend(agent)
    try:
        yield
    finally:
        cap, _ = _get_in_process_children_limits()
        _SLOTS.take_back(agent, cap, lambda: _stopped(agent))


def in_process_children_running() -> int:
    """Children running as threads of this process now (diagnostics)."""
    return _SLOTS.running()
