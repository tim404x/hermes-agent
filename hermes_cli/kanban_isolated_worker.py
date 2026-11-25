"""Isolated Kanban worker entrypoint: card-local SessionAuthority inside its own systemd unit.

Spawned by the dispatcher inside a dedicated transient systemd user service
(``hermes-kanban-w-<hash>-<task_id>-r<run_id>.service``) when
``kanban.worker_plane`` is ``isolated_systemd``.

This process owns an attempt-private ``HERMES_HOME`` (private ``state.db``,
``gateway.lock``, control socket, loopback WebSocket, ``worker-outboxes/``) and
executes the claimed card through the canonical ``session_kanban.run_task`` ->
``SessionAuthority`` -> ``execute_managed`` -> ``agent.managed_worker`` pipeline.
It never writes to the assignee profile's shared ``state.db`` or depends on the
shared profile gateway's lifetime.
"""
from __future__ import annotations

from importlib import import_module

# Spawned as a bare ``sys.executable -m``: in a PM install that interpreter carries no dependencies
# until hermes_bootstrap selects the committed environment, so it must be the first import.
try:
    import_module('hermes_bootstrap')
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap" and not "hermes_bootstrap".startswith(f"{exc.name}."):
        raise

import asyncio
import contextlib
import logging
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class _CardOwnerConnection:
    """In-process native owner connection for ``session_kanban.run_task``."""

    native_owner = True

    def __init__(self, authority: Any, actor: Any) -> None:
        self.authority = authority
        self.actor = actor

    async def resume(self, ref: Any, _params: dict) -> None:
        from gateway.config import Platform
        from gateway.session_policy import policy_for_source, refresh_profile_hooks

        await self.authority.attach(self.actor, ref, history=False)
        live = self.authority.sessions.get(ref.session_id)
        if live is not None and live.source is not None and live.source.platform == Platform.LOCAL:
            policy = policy_for_source(self.authority.runner, live.source)
            await asyncio.to_thread(refresh_profile_hooks, policy)


def _cleanup_launch_env_file(path_str: str) -> None:
    if not path_str:
        return
    with contextlib.suppress(OSError):
        Path(path_str).unlink(missing_ok=True)


def _strip_task_env() -> None:
    """Remove per-card identity env vars before initializing the card-local owner."""
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_") and key not in (
            "HERMES_KANBAN_HOME",
            "HERMES_KANBAN_BUSY_TIMEOUT_MS",
        ):
            os.environ.pop(key, None)
    for key in ("HERMES_SESSION_SOURCE", "HERMES_TENANT", "TERMINAL_CWD"):
        os.environ.pop(key, None)


def _build_identify_handler(runner: Any):
    from gateway.control_socket import build_identify_payload

    descriptor = runner.session_runtime_descriptor

    def _identify() -> dict[str, Any]:
        payload = build_identify_payload()
        payload.update({
            key: descriptor[key]
            for key in (
                "instance_id",
                "runtime_protocol",
                "authority_epoch",
                "state",
                "api_origin",
                "served_profiles",
                "parked_profiles",
                "capabilities",
            )
            if key in descriptor
        })
        if getattr(runner, "_draining", False):
            payload["state"] = "draining"
            payload["capabilities"] = []
        payload["supervisor"] = "systemd"
        return payload

    return _identify


async def _wait_for_admission_settlement(
    store: Any,
    admission_id: str,
    stop_requested: asyncio.Event,
    authority: Any,
) -> dict[str, Any]:
    """Poll the attempt-private admission ledger until ``terminal`` or ``unknown``."""
    from gateway.run_runtime import stop_authority_turns

    stop_sent = False
    while True:
        if stop_requested.is_set() and not stop_sent:
            stop_sent = True
            stop_authority_turns(authority, in_process=True, reason="kanban_worker_stopped")
        row = await asyncio.to_thread(store.admissions.get, admission_id=admission_id)
        if row is not None and row.status in {"terminal", "unknown"}:
            return {"admission_id": admission_id, "status": row.status, "outcome": row.outcome}
        await asyncio.sleep(0.2)


async def _execute_card_with_local_owner(
    *,
    params: dict[str, Any],
    board_db: Path,
    assignee_home: Path,
    attempt_home: Path,
    worker_plane_meta: dict[str, Any],
) -> int:
    from gateway import run_api
    from gateway.config import GatewayConfig
    from gateway.control_resolve import resolve_profile_handler
    from gateway.control_socket import GatewayControlServer
    from gateway.run import GatewayRunner
    from gateway.run_runtime import _retire_profile_authority, initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_contract import SessionActor
    from gateway.session_kanban import run_task, worker_result
    from gateway.session_persistence import authority_persistence
    from gateway.session_worker_reservation import owner_worker_subject

    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            with contextlib.suppress(NotImplementedError, ValueError, OSError):
                loop.add_signal_handler(sig, stop_requested.set)

    control_server: Optional[GatewayControlServer] = None
    api_handle = None
    runner: Optional[GatewayRunner] = None
    authority = None

    process_ownership.reserve([attempt_home])
    try:
        runner = GatewayRunner(config=GatewayConfig(multiplex_profiles=False))
        await initialize_gateway_runtime(runner)
        authority = runner.session_authority
        authority.source_profile_home = str(assignee_home)
        authority.kanban_worker_plane_meta = dict(worker_plane_meta)

        identify = _build_identify_handler(runner)
        control_server = GatewayControlServer(
            home=attempt_home,
            verb_handlers={"identify": identify},
        )
        control_server.ticket_store = runner.session_ticket_store
        control_server.resolve_handler = resolve_profile_handler(identify)
        if not await control_server.start():
            raise RuntimeError(f"failed to start card-local control socket in {attempt_home}")
        runner.session_control_server = control_server

        api_handle = await run_api.start_gateway_api(runner)
        runner.session_api = api_handle
        runner.session_runtime_descriptor.update({
            "api_origin": api_handle.api_origin,
            "state": "ready",
            "capabilities": ["session-authority-v1"],
        })

        actor = SessionActor(
            subject=owner_worker_subject(),
            profile_id=str(attempt_home),
            transport_id=f"kanban-isolated:{uuid.uuid4().hex}",
            capabilities=frozenset({
                "session:create",
                "session:read",
                "session:submit",
                "session:control",
                "session:respond",
                "worker:adopt",
            }),
        )
        conn = _CardOwnerConnection(authority, actor)
        accepted = await run_task(conn, {**params, "db": str(board_db)})
        session_id = accepted["session_id"]
        admission_id = accepted["receipt"]["admission_id"]
        print(f"Session: {session_id}", file=sys.stderr, flush=True)
        if worker_plane_meta.get("systemd_unit"):
            print(f"Worker unit: {worker_plane_meta['systemd_unit']}", file=sys.stderr, flush=True)
        print(f"Attempt DB: {attempt_home / 'state.db'}", file=sys.stderr, flush=True)

        store = authority_persistence(authority)
        receipt = await _wait_for_admission_settlement(store, admission_id, stop_requested, authority)
        if receipt["status"] == "unknown":
            print("Kanban execution unknown; retained by card-local owner", file=sys.stderr, flush=True)
            return 1
        result = worker_result(board_db, params)
        if result.get("last_output"):
            print(result["last_output"], flush=True)
        return int(result.get("exit_code", 1))
    finally:
        if runner is not None:
            runner._draining = True
            if hasattr(runner, "session_runtime_descriptor"):
                runner.session_runtime_descriptor["state"] = "draining"
                runner.session_runtime_descriptor["capabilities"] = []
        if authority is not None:
            with contextlib.suppress(Exception):
                await _retire_profile_authority(authority, timeout=20.0)
        if api_handle is not None:
            with contextlib.suppress(Exception):
                await run_api.stop_gateway_api(api_handle)
        if control_server is not None:
            with contextlib.suppress(Exception):
                await control_server.stop()
        if authority is not None and getattr(authority, "store_reference", None) is not None:
            with contextlib.suppress(Exception):
                authority.store_reference.close()
        if runner is not None and getattr(runner, "session_store", None) is not None:
            with contextlib.suppress(Exception):
                runner.session_store.close()
        with contextlib.suppress(Exception):
            process_ownership.release(attempt_home)


def _run_isolated() -> int:
    try:
        params = {
            "board": os.environ["HERMES_KANBAN_BOARD"],
            "task_id": os.environ["HERMES_KANBAN_TASK"],
            "run_id": int(os.environ["HERMES_KANBAN_RUN_ID"]),
            "claim_lock": os.environ["HERMES_KANBAN_CLAIM_LOCK"],
        }
        board_db = Path(os.environ["HERMES_KANBAN_DB"]).expanduser().resolve()
        assignee_home = Path(os.environ["HERMES_KANBAN_ISOLATED_PROFILE_HOME"]).expanduser().resolve()
        attempt_home = Path(os.environ["HERMES_KANBAN_ATTEMPT_HOME"]).expanduser().resolve()
        unit_name = os.environ.get("HERMES_KANBAN_WORKER_UNIT", "").strip()
        boot_id = os.environ.get("HERMES_KANBAN_WORKER_BOOT_ID", "").strip()
        launch_env_file = os.environ.get("HERMES_KANBAN_LAUNCH_ENV_FILE", "").strip()
        invocation_id = os.environ.get("INVOCATION_ID", "").strip()
    except (KeyError, ValueError, OSError) as exc:
        print(f"Kanban isolated worker refused: {exc}", file=sys.stderr, flush=True)
        return 1

    _cleanup_launch_env_file(launch_env_file)
    _strip_task_env()
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch import _process_fingerprint, adopt_worker_pid
    from hermes_cli.kanban_worker_plane import (
        WORKER_PLANE_ISOLATED_SYSTEMD,
        UnitState, attempt_home_dir, isolated_unit_state, isolated_worker_unit_name,
        read_current_cgroup_path,
    )
    from hermes_constants import apply_subprocess_home_env

    apply_subprocess_home_env(os.environ)
    if (unit_name != isolated_worker_unit_name(board_db, params['task_id'], params['run_id'])
            or attempt_home != attempt_home_dir(board_db, params['task_id'], params['run_id'])
            or os.environ.get('HERMES_HOME') != str(attempt_home)):
        raise RuntimeError('mismatched isolated attempt home or unit')
    worker_plane_meta = {
        "worker_plane": WORKER_PLANE_ISOLATED_SYSTEMD,
        "systemd_unit": unit_name,
        "invocation_id": invocation_id,
        "boot_id": boot_id,
        "attempt_home": str(attempt_home),
        "profile_home": str(assignee_home),
    }
    cgroup = read_current_cgroup_path()
    if isolated_unit_state(dict(worker_plane_meta, pid=os.getpid(), started_at=_process_fingerprint(os.getpid()))) != UnitState.LIVE:
        raise RuntimeError('cannot verify live isolated unit identity')
    if cgroup:
        worker_plane_meta["cgroup"] = cgroup

    # Claim CAS + fallback PID adoption if the dispatcher died right after systemd-run returned.
    with connect_closing(board_db) as conn:
        if not adopt_worker_pid(
            conn,
            params["task_id"],
            params["run_id"],
            os.getpid(),
            worker_plane_meta=worker_plane_meta,
        ):
            print("Kanban isolated worker refused: claim no longer active", file=sys.stderr, flush=True)
            return 1

    try:
        return asyncio.run(
            _execute_card_with_local_owner(
                params=params,
                board_db=board_db,
                assignee_home=assignee_home,
                attempt_home=attempt_home,
                worker_plane_meta=worker_plane_meta,
            )
        )
    except Exception as exc:
        logger.exception("Kanban isolated worker failed")
        print(f"Kanban isolated worker failed: {exc}", file=sys.stderr, flush=True)
        return 1


def main() -> int:
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

    rc = 1
    try:
        rc = _run_isolated()
    finally:
        print(f"\n{KANBAN_WORKER_EXIT_TRAILER}{int(rc)}", file=sys.stderr, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
