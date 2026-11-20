"""Stale-while-revalidate holder for the unscoped ``/api/status`` body.

The Desktop polls ``/api/status`` about once a second (with 2 s and 8 s client timeouts), and one
computed body costs ~10 worker-thread hops (liveness ladder, gateway config, topology, a session
listing, storage and pressure probes). With other threads holding the GIL that compute stretched to
seconds, so the poll timed out ("Timed out connecting to Hermes backend") while the process was
healthy. The serving process therefore answers from the last body:

- younger than ``fresh_s``: returned as is, no compute;
- older (up to ``max_stale_s``): returned at once AND one background refresh starts;
- none yet, or older than ``max_stale_s``: the caller waits for the refresh (one compute however
  many callers are waiting); while a refresh is already running, the last body answers instead.

Only the real server arms it (``web_server._start_server_runtime``); unarmed (tests, in-process and
embedded callers) every call computes, exactly as before. Each caller gets its own deep copy, so the
per-caller redaction in ``get_status`` never touches the shared body.

The refresh runs in a fresh ``contextvars.Context``: the body describes the launch profile whichever
request happened to start it (``get_status`` keeps requests scoped to another home off the snapshot).
A refresh that has not answered within ``compute_timeout_s`` fails like any other compute error, so
one wedged probe cannot hold every later caller on the same pending task.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

_log = logging.getLogger("hermes_cli.web_server")


class StatusSnapshot:
    def __init__(self, fresh_s: float = 3.0, max_stale_s: float = 30.0, compute_timeout_s: float = 20.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.fresh_s, self.max_stale_s, self.compute_timeout_s = fresh_s, max_stale_s, compute_timeout_s
        self._clock = clock
        self.armed = False
        self._body: Optional[dict[str, Any]] = None
        self._at = 0.0
        self._refresh: Optional[asyncio.Future] = None
        self._generation = 0

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        """Back to compute-every-call; a refresh still in flight can no longer store its body."""
        self._generation += 1
        self.armed, self._body, self._at, self._refresh = False, None, 0.0, None

    async def get(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        if not self.armed:
            return await compute()
        body, age = self._body, self._clock() - self._at
        if body is not None and (age < self.max_stale_s or self._in_flight()):
            # Past max_stale_s with a refresh already running (a slow or wedged probe), the last body
            # still answers: awaiting that refresh would hold every poll until compute_timeout_s.
            if age >= self.fresh_s:
                self._start(compute)
            return copy.deepcopy(body)
        # Shielded: a caller that disconnects must not cancel the refresh other callers await.
        return copy.deepcopy(await asyncio.shield(self._start(compute)))

    def _in_flight(self) -> bool:
        task = self._refresh
        return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()

    def _start(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Future:
        task = self._refresh
        loop = asyncio.get_running_loop()
        if task is None or task.done() or task.get_loop() is not loop:
            task = self._refresh = loop.create_task(
                self._run(compute, self._generation), context=contextvars.Context())
            task.add_done_callback(_log_refresh_failure)
        return task

    async def _run(self, compute: Callable[[], Awaitable[dict[str, Any]]], generation: int) -> dict[str, Any]:
        body = await asyncio.wait_for(compute(), self.compute_timeout_s)
        if generation == self._generation:
            self._body, self._at = body, self._clock()
        return body


def _log_refresh_failure(task: asyncio.Future) -> None:
    # A failed background refresh keeps serving the last body until it is max_stale_s old; a
    # caller awaiting a cold refresh gets the exception itself (the route's 500, as before).
    if not task.cancelled() and task.exception() is not None:
        _log.warning("/api/status refresh failed: %s", task.exception())


STATUS_SNAPSHOT = StatusSnapshot()
