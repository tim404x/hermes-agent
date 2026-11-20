"""Launch waves for subagents (``delegation.launch_wave_size`` / ``delegation.launch_wave_seconds``).

A provider rate-limits sudden ramps, not only steady load: 80 children whose first requests (each a fresh
prompt-cache write) land in the same second draw 429s that a gradual start would not. With a wave size N, at
most N subagents START in any ``launch_wave_seconds`` window across this process, nested batches included,
and the rest wait for their wave in launch order. Only starts are paced: a running child is never throttled.
Size 0 (the default) turns it off.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from tools.delegate_tool_config import _get_launch_waves

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

_POLL_S = 0.05


class LaunchPacer:
    """Sliding-window start schedule: launch k starts no earlier than launch k-size plus the window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock, self._lock, self._starts = clock, threading.Lock(), deque()

    def reserve(self, size: int, window_s: float) -> float:
        """Record the next launch and return when it may start (now, or when its wave opens)."""
        with self._lock:
            now = self._clock()
            start = now if len(self._starts) < size else max(now, self._starts[-size] + window_s)
            self._starts.append(start)
            while len(self._starts) > size:
                self._starts.popleft()
            return start

    def wait_turn(self, size: int, window_s: float, stopped: Callable[[], bool]) -> bool:
        """Block until this launch's wave opens; False when ``stopped()`` turns true first."""
        start = self.reserve(size, window_s)
        if start > self._clock():
            logger.info("delegate: subagent launch held %.1fs for its wave (launch_wave_size=%d)",
                        start - self._clock(), size)
        while (remaining := start - self._clock()) > 0:
            if stopped():
                return False
            time.sleep(min(_POLL_S, remaining))
        return True


_PACER = LaunchPacer()  # one schedule per process: nested batches share the provider's ramp


def _stopped(agent: Any) -> bool:
    return getattr(agent, "_interrupt_requested", False) is True


def wait_for_launch_wave(child: Any, parent: Optional[Any] = None) -> bool:
    """True when ``child`` may start (waves off, or its wave opened); False when it, or ``parent`` when given,
    was stopped while it waited (the caller releases it unstarted)."""
    size, window_s = _get_launch_waves()
    if size <= 0 or window_s <= 0:
        return True
    return _PACER.wait_turn(size, window_s, lambda: _stopped(child) or (parent is not None and _stopped(parent)))
