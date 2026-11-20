"""Stale-while-revalidate holder for the unscoped ``/api/status`` body.

The Desktop polls ``/api/status`` about once a second (with 2 s and 8 s client timeouts), and one
computed body costs ~10 worker-thread hops (liveness ladder, gateway config, topology, a session
listing, storage and pressure probes). With other threads holding the GIL that compute stretched to
seconds, so the poll timed out ("Timed out connecting to Hermes backend") while the process was
healthy. The serving process therefore answers from the last body:

- younger than ``fresh_s``: returned as is, no compute;
- older (up to ``max_stale_s``): returned at once AND one background refresh starts;
- none yet, or older than ``max_stale_s``: the caller waits for the refresh (one compute however
  many callers are waiting); while a refresh is already running, the last body answers instead, up
  to ``max_stale_s + compute_timeout_s`` old.

Only the real server arms it (``web_server._start_server_runtime``); unarmed (tests, in-process and
embedded callers) every call computes, exactly as before. Each caller gets its own deep copy, so the
per-caller redaction in ``get_status`` never touches the shared body.

The refresh runs in a fresh ``contextvars.Context``: the body describes the launch profile whichever
request happened to start it (``get_status`` keeps requests scoped to another home off the snapshot).
A refresh that has not answered within ``compute_timeout_s`` fails like any other compute error, so
one wedged probe cannot hold every later caller on the same pending task.

A write through this server that the body reports (settings, .env, gateway control, messaging
platforms, profiles, provider sign-in, the auth gate: ``StatusWriteInvalidation`` ->
``invalidate_status``) retires the body and any refresh started before it: the next caller waits for
a compute that starts after the write, at most ``invalidate_wait_s`` (past it, or if that compute
fails, the last body answers while the refresh finishes). The WS ``config.set`` keys (model, reasoning,
display, cwd...) are not in the body. Writes by other processes still show within ``fresh_s``.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import itertools
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS

_log = logging.getLogger("hermes_cli.web_server")


class StatusSnapshot:
    def __init__(self, fresh_s: float = 3.0, max_stale_s: float = 30.0, compute_timeout_s: float = 20.0,
                 clock: Callable[[], float] = time.monotonic, invalidate_wait_s: float = 1.5) -> None:
        self.fresh_s, self.max_stale_s, self.compute_timeout_s = fresh_s, max_stale_s, compute_timeout_s
        self.invalidate_wait_s = invalidate_wait_s
        self._clock = clock
        self.armed = False
        self._body: Optional[dict[str, Any]] = None
        self._at = 0.0
        self._refresh: Optional[asyncio.Future] = None
        # Generations: a body or refresh from an older one never answers a later caller. ``next()`` on a count is one
        # atomic step, so ``invalidate`` is safe from a worker thread or the WS gateway's thread.
        self._generations = itertools.count(1)
        self._generation = 0
        self._body_generation = 0
        self._refresh_generation = 0

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        """Back to compute-every-call; a refresh still in flight can no longer store its body."""
        self._generation = next(self._generations)
        self.armed, self._body, self._at, self._refresh = False, None, 0.0, None

    def invalidate(self) -> None:
        """This process just wrote something the body reports: the body and any refresh already running are
        retired; the next caller waits (bounded) for a compute that starts after this call."""
        self._generation = next(self._generations)

    async def get(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        if not self.armed:
            return await compute()
        body, age = self._body, self._clock() - self._at
        written = body is not None and self._body_generation != self._generation
        if body is not None and not written and (
                age < self.max_stale_s
                or (self._in_flight() and age < self.max_stale_s + self.compute_timeout_s)):
            # Just past max_stale_s with a refresh already running (a slow probe), the last body still
            # answers rather than holding the poll. Never older than one refresh's timeout beyond that:
            # a probe that keeps wedging must surface as a failing status, not as a frozen healthy one.
            if age >= self.fresh_s:
                self._start(compute)
            return copy.deepcopy(body)
        # Shielded: a caller that disconnects must not cancel the refresh other callers await.
        task = asyncio.shield(self._start(compute))
        if not written or age >= self.max_stale_s + self.compute_timeout_s:
            # (A body that old never answers in place of a refresh: a wedging probe surfaces, as above.)
            return copy.deepcopy(await task)
        # Written since the body: read-your-writes, but never held past invalidate_wait_s (the Desktop's short
        # status timeout is 2 s), and a failed compute answers with the last body as a background one would.
        try:
            return copy.deepcopy(await asyncio.wait_for(task, self.invalidate_wait_s))
        except asyncio.CancelledError:
            raise
        except Exception:  # health: allow BLE001 -- timeout or a failed compute: the last body answers, the refresh logs its own failure
            return copy.deepcopy(body)

    def _in_flight(self) -> bool:
        task = self._refresh
        return task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop()

    def _start(self, compute: Callable[[], Awaitable[dict[str, Any]]]) -> asyncio.Future:
        task = self._refresh
        loop = asyncio.get_running_loop()
        generation = self._generation
        # A refresh started before the last invalidate() may have read the old state: start a new one.
        if task is None or task.done() or task.get_loop() is not loop or self._refresh_generation != generation:
            self._refresh_generation = generation
            task = self._refresh = loop.create_task(self._run(compute, generation), context=contextvars.Context())
            task.add_done_callback(_log_refresh_failure)
        return task

    async def _run(self, compute: Callable[[], Awaitable[dict[str, Any]]], generation: int) -> dict[str, Any]:
        body = await asyncio.wait_for(compute(), self.compute_timeout_s)
        if generation == self._generation:
            self._body, self._at, self._body_generation = body, self._clock(), generation
        return body


def _log_refresh_failure(task: asyncio.Future) -> None:
    # A failed background refresh keeps serving the last body until it is max_stale_s old; a
    # caller awaiting a cold refresh gets the exception itself (the route's 500, as before).
    if not task.cancelled() and task.exception() is not None:
        _log.warning("/api/status refresh failed: %s", task.exception())


STATUS_SNAPSHOT = StatusSnapshot()


def invalidate_status() -> None:
    """A write this process made may change ``/api/status``: retire the snapshot and the gateway topology's TTL
    cache (else a gateway or profile toggle would still read the old topology for up to 10 s). Any thread; a
    process that never loaded the dashboard modules has nothing to retire."""
    STATUS_SNAPSHOT.invalidate()
    gateway = sys.modules.get("hermes_cli.web_server_gateway")
    if gateway is not None:
        try:
            gateway.invalidate_topology_cache()
        except Exception:  # health: allow BLE001 -- the snapshot is retired either way; the TTL still bounds the cache
            _log.debug("topology cache invalidate failed", exc_info=True)


# Mutating requests under these paths change what /api/status reports (config + its version, .env, gateway
# lifecycle, messaging platforms, profiles, provider sign-in, the webapp auth gate, an update). Not every
# write: uploads, transcripts, logs and the like are frequent and change nothing here, and each retired body
# costs a full compute.
STATUS_WRITE_PATH_PREFIXES: tuple[str, ...] = (
    "/api/config", "/api/env", "/api/gateway/", "/api/messaging/", "/api/profiles", "/api/providers/",
    "/api/webapp-access/", "/api/hermes/update", "/api/ops/config-migrate", "/api/ops/import",
    "/api/ops/transfer/restore",
)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def is_status_write(method: str, path: str) -> bool:
    return method.upper() in _WRITE_METHODS and path.startswith(STATUS_WRITE_PATH_PREFIXES)


class StatusWriteInvalidation:
    """ASGI middleware: after a status-relevant write (``is_status_write``) the snapshot is retired when the response
    starts (the handler's write is done, and the client's next poll cannot overtake it), or when the request ends if
    it failed before answering (it may still have written). A refused request (any 4xx: auth, validation, unknown
    route or method) wrote nothing and retires nothing, and neither does a public path (``PUBLIC_API_PATHS``), so an
    anonymous caller cannot keep the shared snapshot cold."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope.get("type") != "http" or path in PUBLIC_API_PATHS
                or not is_status_write(scope.get("method", ""), path)):
            return await self.app(scope, receive, send)
        started = False

        async def send_and_retire(message):
            nonlocal started
            if message.get("type") == "http.response.start" and not started:
                started = True
                if not 400 <= int(message.get("status") or 500) < 500:
                    invalidate_status()
            await send(message)

        try:
            return await self.app(scope, receive, send_and_retire)
        finally:
            if not started:
                invalidate_status()
