"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger
from hermes_cli.kanban_dispatch.results import DispatchResult, describe_suppression


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch

_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass(frozen=True)
class _DispatcherSettings:
    """Live settings shared by all boards for one tick. Only ``dispatch_in_gateway`` is boot-only."""

    interval: float
    max_spawn: Optional[int]
    max_in_progress: Optional[int]
    failure_limit: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]


def _dispatch_interval(kanban_cfg: dict, fallback: float) -> float:
    raw = kanban_cfg.get("dispatch_interval_seconds")
    try:
        interval = float(raw or 60)
        if math.isfinite(interval):
            return max(interval, 1.0)
    except (ValueError, TypeError, OverflowError):
        pass
    logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r; using %g", raw, fallback)
    return fallback


def _resolve_dispatcher_settings(
    kanban_cfg: dict, kb: Any, *, log: bool = True, previous: Optional[_DispatcherSettings] = None,
) -> _DispatcherSettings:
    """Resolve one snapshot; refreshes omit boot INFO logs, not validation warnings."""
    interval = _dispatch_interval(kanban_cfg, previous.interval if previous else 60.0)
    max_spawn = _positive_int_setting(
        kanban_cfg, "max_spawn", log=log, fallback=previous.max_spawn if previous else None, strict=True,
    )

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress", log=log)
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if log and max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if log and default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile", log=log),
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings, *, kanban_cfg: Optional[dict] = None) -> None:
        self.kb = kb
        self.settings = settings
        self._applied_kanban = deepcopy(kanban_cfg)
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}

    def _board_slugs(self) -> list:
        return _board_slugs(self.kb)

    @property
    def max_in_progress_source(self) -> str:
        if self.settings.max_in_progress is None:
            return "uncapped"
        if _kbd().configured_max_in_progress(self._applied_kanban or {}) is not None:
            return "config"
        return "memory-default"

    def refresh_settings(self) -> None:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config_backend import ConfigBackendUnavailable
        from hermes_cli.config_read_errors import FailedConfigRead

        try:
            cfg = load_config_readonly()
            if isinstance(cfg, FailedConfigRead):
                logger.debug("kanban dispatcher: config read failed (%s); keeping previous settings",
                             cfg.read_error)
                return
            kanban = cfg.get("kanban")
            if not isinstance(kanban, dict):
                return
            snapshot = self._applied_kanban
            if kanban == snapshot and type(kanban.get("max_spawn")) is type(snapshot.get("max_spawn")):
                if self.max_in_progress_source == "config":
                    return
                cap = _kbd().resolve_max_in_progress(None)
                if cap == self.settings.max_in_progress:
                    return
                settings = replace(self.settings, max_in_progress=cap)
            else:
                snapshot = deepcopy(kanban)
                settings = _resolve_dispatcher_settings(snapshot, self.kb, log=False, previous=self.settings)
        except (Exception, ConfigBackendUnavailable):  # the latter is a SystemExit on purpose
            logger.debug("kanban dispatcher: could not refresh settings; keeping previous values", exc_info=True)
            return

        for setting in fields(settings):
            previous, value = getattr(self.settings, setting.name), getattr(settings, setting.name)
            if value != previous:
                logger.info(
                    "kanban dispatcher: %s changed %s -> %s (config edit applied live)", setting.name,
                    "unset" if previous is None else previous, "unset" if value is None else value,
                )
        self.settings = settings
        self._applied_kanban = snapshot

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        from hermes_cli import kanban_db as _kb
        with _kb.pin_first_board_resolution():
            path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True

    def tick_once_for_board(self, slug: str) -> Optional[object]:
        """Run a standalone tick for one board, including live settings."""
        self.refresh_settings()
        return self._dispatch_board(slug)

    def _dispatch_board(self, slug: str) -> Optional[object]:
        """Open the per-board DB so boards never share a connection or claim."""
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            # Pin-first: the tick is machine flow — on a box whose env pins
            # HERMES_KANBAN_DB every enumerated slug must resolve to the pinned
            # file, or the dispatcher reads per-slug DBs nobody writes.
            from hermes_cli import kanban_db as _kb
            with _kb.pin_first_board_resolution():
                conn = _kbc().connect(board=slug)
                return _kbd().dispatch_once(conn, board=slug, **kwargs)
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def tick_once(self) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs."""
        self.refresh_settings()
        return [(slug, self._dispatch_board(slug)) for slug in self._board_slugs()]

    def ready_nonempty(self) -> bool:
        """Is there unclaimed ready or review work on ANY board the dispatcher could spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        from hermes_cli import kanban_db as _kb
        with _kb.pin_first_board_resolution():
            for slug in self._board_slugs():
                conn = None
                try:
                    conn = _kbc().connect(board=slug)
                    if kbd.has_spawnable_ready(conn, self.settings.default_assignee) or (
                        _review_probe and kbd.has_spawnable_review(conn)
                    ):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        with contextlib.suppress(Exception):
                            conn.close()
        return False

    def auto_decompose_tick(self, auto_decompose_per_tick: int) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
            from hermes_cli.kanban_db import scoped_current_board
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        from hermes_cli import kanban_db as _kb
        with _default_profile_secret_scope(), _kb.pin_first_board_resolution():
            for slug in self._board_slugs():
                if attempted >= auto_decompose_per_tick:
                    break
                # The decomposer connects with no board kwarg. Pin the board for this context only: an
                # env pin is process-wide, so for the whole aux-LLM call every gateway turn, kanban tool
                # and spawned subprocess that names no board would resolve this one instead.
                with scoped_current_board(slug):
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        successes += self._decompose_one(_decomp, slug, tid)
        return successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer", source="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


@contextlib.contextmanager
def _default_profile_secret_scope():
    """Install the gateway launch profile's secret scope while multiplexing is on.

    The tick runs via ``_to_thread_process_service`` in a fresh context, so no
    per-turn scope exists and ``get_secret`` fails closed. The decomposer's aux
    LLM reads ``auxiliary.*`` from ``get_hermes_home()``, so its credentials come
    from that same home. No-op for single-profile gateways.
    """
    from agent.secret_scope import is_multiplex_active, reset_secret_scope, set_secret_scope
    from hermes_constants import get_hermes_home
    from tui_gateway.launch_profile_policy import owner_secret_scope

    if not is_multiplex_active():
        yield
        return
    token = set_secret_scope(owner_secret_scope(Path(get_hermes_home())), profile_home=str(get_hermes_home()))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _stuck_warning(
    bad_ticks: int, results: list[Optional[DispatchResult]], max_in_progress_source: str,
    per_profile_limit: Optional[int] = None,
) -> str:
    def binding_cap(name: str, running: int, limit: int) -> str:
        source = max_in_progress_source if name == "max_in_progress" else "config"
        return (f"Binding cap: {name}={limit} (source: {source}, running {running}). "
                f"Raise kanban.{name} in config.yaml (applies live)")

    reason = describe_suppression(results, format_capacity=binding_cap)
    if per_profile_limit is not None and any(res and res.skipped_per_profile_capped for res in results):
        reason += f". Binding cap: max_in_progress_per_profile={per_profile_limit} (source: config, applies live)"
    detail = f"{reason}." if reason else (
        "Check profile health (venv, PATH, credentials) and `hermes kanban list --status ready`."
    )
    return (f"kanban dispatcher stuck: ready queue non-empty for {bad_ticks} consecutive ticks "
            f"but 0 workers spawned. {detail}")


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is not None and getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned
