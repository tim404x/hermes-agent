"""Isolated systemd worker plane for Kanban card execution.

When ``kanban.worker_plane`` is set to ``isolated_systemd`` (explicit opt-in;
default ``profile_owner`` preserves existing shared-gateway execution), each
claimed Kanban attempt runs inside its own transient systemd user service
(``hermes-kanban-w-<board_hash>-<task_id>-r<run_id>.service``) backed by an
attempt-private ``HERMES_HOME`` and a card-local ``SessionAuthority``.

Restarting the profile gateway neither kills the transient worker unit nor
touches the attempt's private ``state.db``. Live identity binds
``(board, task_id, run_id, pid, birth/started_at, systemd_unit, invocation_id,
boot_id)`` — never bare PID existence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli.kanban_isolation.home import (
    format_bind_readonly_path,
    prepare_attempt_home_plan,
    validate_systemd_bind_path,
)
from hermes_cli.kanban_isolation.mounts import readonly_bind_probe_script
from hermes_platform.host.facts import os_family
from hermes_platform.resolver import locate_command

WORKER_PLANE_PROFILE_OWNER = "profile_owner"
WORKER_PLANE_ISOLATED_SYSTEMD = "isolated_systemd"
VALID_WORKER_PLANES = frozenset({
    WORKER_PLANE_PROFILE_OWNER,
    WORKER_PLANE_ISOLATED_SYSTEMD,
})

_IS_LINUX = os_family() == 'linux'
_UNIT_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_ACTIVE_UNIT_STATES = frozenset({"active", "activating", "reloading", "deactivating"})

_PROBE_LOCK = threading.Lock()
_PROBE_AVAILABLE: Optional[bool] = None
_PROBE_REASON: str = ""
_PROBE_AT: float = 0.0
_PROBE_TTL_SECONDS = 30.0
_PROBE_NO_EXPAND = True


@dataclass(frozen=True)
class IsolatedLaunchReceipt:
    """Durable launch identity of one isolated systemd Kanban worker attempt."""

    pid: int
    started_at: str
    unit_name: str
    invocation_id: str
    boot_id: str
    attempt_home: str
    profile_home: str
    worker_plane: str = WORKER_PLANE_ISOLATED_SYSTEMD
    readonly_assets: tuple[str, ...] = ()

    def event_metadata(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'started_at': self.started_at,
            "worker_plane": self.worker_plane,
            "systemd_unit": self.unit_name,
            "invocation_id": self.invocation_id,
            "boot_id": self.boot_id,
            "attempt_home": self.attempt_home,
            "profile_home": self.profile_home,
        }
        if self.readonly_assets:
            payload["readonly_assets"] = list(self.readonly_assets)
        return payload


class LaunchUnconfirmed(RuntimeError):
    """The unit may already exist: leave the durable claim for reconciliation."""


def _record_launch_intent(database, task, metadata):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing(database) as conn, kb.write_txn(conn):
        if not conn.execute("SELECT 1 FROM tasks WHERE id=? AND current_run_id=? AND claim_lock=? AND status='running'",
                            (task.id, task.current_run_id, task.claim_lock)).fetchone():
            raise LaunchUnconfirmed('claim changed before isolated worker launch')
        kb._append_event(conn, task.id, 'unit_requested', metadata, run_id=task.current_run_id)


def reset_systemd_service_probe_cache() -> None:
    """Clear the cached systemd transient-service availability probe (tests)."""
    global _PROBE_AVAILABLE, _PROBE_REASON, _PROBE_AT, _PROBE_NO_EXPAND
    with _PROBE_LOCK:
        _PROBE_AVAILABLE = None
        _PROBE_REASON = ""
        _PROBE_AT = 0.0
        _PROBE_NO_EXPAND = True


def resolve_worker_plane(kanban_cfg: Optional[Mapping[str, Any]] = None) -> str:
    """Resolve ``kanban.worker_plane`` from config; default ``profile_owner``.

    Unrecognized non-empty values raise ``ValueError`` (fail closed).
    """
    if kanban_cfg is None:
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.kanban_worker_preflight import ProfileConfigUnavailable
        try:
            kanban_cfg = load_user_config_effective(fail_closed=True).get('kanban') or {}
        except Exception as exc:
            raise ProfileConfigUnavailable('cannot verify configured Kanban worker plane') from exc
    raw = kanban_cfg.get('worker_plane')
    if raw is None:
        return WORKER_PLANE_PROFILE_OWNER
    value = str(raw).strip().lower()
    if not value:
        return WORKER_PLANE_PROFILE_OWNER
    if value not in VALID_WORKER_PLANES:
        raise ValueError(
            f"unsupported kanban.worker_plane={raw!r}; expected one of "
            f"{sorted(VALID_WORKER_PLANES)}"
        )
    return value


def isolated_worker_unit_name(board_db: str | Path, task_id: str, run_id: int) -> str:
    """Deterministic, collision-free transient systemd user service name."""
    resolved = str(Path(board_db).expanduser().resolve())
    board_hash = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    safe_task = _UNIT_SAFE_RE.sub("-", str(task_id)).strip("-") or "task"
    safe_task = safe_task[:64]
    return f"hermes-kanban-w-{board_hash}-{safe_task}-r{int(run_id)}.service"


def attempt_root_dir(board_db: str | Path, task_id: str, run_id: int) -> Path:
    """Board-scoped private root directory for one attempt's isolated owner."""
    safe_task = _UNIT_SAFE_RE.sub("-", str(task_id)).strip("-") or "task"
    if safe_task in {'.', '..'}:
        raise ValueError('invalid isolated attempt directory name')
    return Path(board_db).expanduser().resolve().parent / "kanban-worker-plane" / safe_task / f"run-{int(run_id)}"


def attempt_home_dir(board_db: str | Path, task_id: str, run_id: int) -> Path:
    """Attempt-private ``HERMES_HOME`` (owns private state.db, lock, socket, outboxes)."""
    return attempt_root_dir(board_db, task_id, run_id) / "home"


def _systemd_escape_specifiers(value: str) -> str:
    """Escape systemd ``%`` specifiers inside transient unit property values."""
    return str(value).replace("%", "%%")


def _command(name):
    return next(iter(locate_command(name).command), None)


def _bus_env(base_env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    from tools.process_registry import systemd_user_bus_env
    return systemd_user_bus_env(dict(base_env) if base_env is not None else None)


def _cached_probe() -> Optional[tuple[bool, str]]:
    if _PROBE_AVAILABLE is None:
        return None
    if time.monotonic() - _PROBE_AT >= _PROBE_TTL_SECONDS:
        return None
    return _PROBE_AVAILABLE, _PROBE_REASON


def _probe_base_dir(probe_env: Mapping[str, str]) -> Optional[Path]:
    for candidate in (probe_env.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}", tempfile.gettempdir()):
        if not candidate:
            continue
        try:
            resolved = Path(candidate).expanduser().resolve()
            validate_systemd_bind_path(resolved)
        except (OSError, ValueError):
            continue
        if resolved.is_dir() and os.access(resolved, os.W_OK | os.X_OK):
            return resolved
    return None


def systemd_user_service_available(
    env: Optional[Mapping[str, str]] = None,
) -> tuple[bool, str]:
    """Return ``(available, reason)`` for launching transient systemd user services with RO mounts."""
    global _PROBE_AVAILABLE, _PROBE_REASON, _PROBE_AT, _PROBE_NO_EXPAND
    if not _IS_LINUX:
        return False, "isolated_systemd worker plane requires Linux systemd user manager"
    cached = _cached_probe()
    if cached is not None:
        return cached
    with _PROBE_LOCK:
        cached = _cached_probe()
        if cached is not None:
            return cached
        run_bin = _command('systemd-run')
        ctl_bin = _command('systemctl')
        if not run_bin or not ctl_bin:
            _PROBE_AVAILABLE = False
            _PROBE_REASON = "systemd-run or systemctl binary not found on PATH"
            _PROBE_AT = time.monotonic()
            return False, _PROBE_REASON
        probe_env = _bus_env(env)
        if "DBUS_SESSION_BUS_ADDRESS" not in probe_env:
            _PROBE_AVAILABLE = False
            _PROBE_REASON = (
                f"no reachable user D-Bus session at /run/user/{os.getuid()}/bus "
                "(enable linger with `sudo loginctl enable-linger <user>`)"
            )
            _PROBE_AT = time.monotonic()
            return False, _PROBE_REASON
        base_dir = _probe_base_dir(probe_env)
        if base_dir is None:
            _PROBE_AVAILABLE = False
            _PROBE_REASON = "no safe writable runtime directory available for read-only bind-mount probe"
            _PROBE_AT = time.monotonic()
            return False, _PROBE_REASON
        probe_unit = f"hermes-kanban-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}.service"
        available = False
        reason = ""
        try:
            with tempfile.TemporaryDirectory(prefix="hermes-kanban-ro-", dir=base_dir) as tmp_dir:
                os.chmod(tmp_dir, 0o700)
                src = (Path(tmp_dir) / "src").resolve()
                dst = (Path(tmp_dir) / "dst").resolve()
                src.mkdir(mode=0o700)
                dst.mkdir(mode=0o700)
                canary = "canary"
                (src / canary).write_text("ok", encoding="utf-8")
                src_st = src.lstat()
                bind_spec = format_bind_readonly_path(src, dst)
                probe_script = readonly_bind_probe_script()
                for _attempt in range(2):
                    no_expand = ["--expand-environment=no"] if _PROBE_NO_EXPAND else []
                    argv = [
                        run_bin, "--user", "--quiet", "--unit", probe_unit,
                        "--service-type=exec", "--collect", "--wait",
                        "--property", f"BindReadOnlyPaths={bind_spec}",
                        *no_expand,
                        "--", sys.executable, "-IS", "-c", probe_script,
                        str(dst), str(src_st.st_dev), str(src_st.st_ino), canary,
                    ]
                    proc = subprocess.run(
                        argv, stdin=subprocess.DEVNULL, capture_output=True,
                        timeout=10, env=probe_env, check=False,
                    )
                    stderr_bytes = proc.stderr or b""
                    if proc.returncode != 0 and _PROBE_NO_EXPAND and b"expand-environment" in stderr_bytes:
                        _PROBE_NO_EXPAND = False
                        continue
                    available = proc.returncode == 0
                    if not available:
                        detail = stderr_bytes.decode("utf-8", "replace").strip() or f"rc={proc.returncode}"
                        reason = f"systemd-run --user read-only bind-mount probe failed: {detail}"
                    break
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            available = False
            reason = f"systemd-run --user read-only bind-mount probe error: {exc}"
        _PROBE_AVAILABLE = available
        _PROBE_REASON = reason
        _PROBE_AT = time.monotonic()
        return available, reason


def read_current_cgroup_path(pid: Optional[int] = None) -> Optional[str]:
    """Read unified cgroup path for *pid* (default self) on Linux, else None."""
    if not _IS_LINUX:
        return None
    target = "self" if pid is None else str(int(pid))
    try:
        raw = Path(f"/proc/{target}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            return parts[2].strip() or None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2].strip():
            return parts[2].strip()
    return None


def inspect_systemd_unit(
    unit_name: str,
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Query systemd user manager properties for *unit_name*."""
    if not _IS_LINUX or not unit_name:
        return {}
    ctl_bin = _command('systemctl')
    if not ctl_bin:
        return {}
    props = "Id,LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup"
    try:
        proc = subprocess.run(
            [ctl_bin, "--user", "show", f"--property={props}", unit_name],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
            env=_bus_env(env), check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    parsed: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            parsed[key.strip()] = val.strip()
    return parsed


def launch_isolated_worker_service(
    *,
    task: Any,
    workspace: str,
    board: Optional[str],
    board_db: Path,
    profile_home: Path,
    env: Mapping[str, str],
    log_path: Path,
) -> IsolatedLaunchReceipt:
    """Start one card-local isolated owner inside its own transient systemd user service.

    Fails closed with ``RestartSafeScopeUnavailable`` whenever transient systemd
    user services are unavailable on this host.
    """
    from gateway.drain_control import current_instantiation_epoch
    from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT, _process_fingerprint
    from tools.process_registry import RestartSafeScopeUnavailable, _worker_memory_max_bytes

    if task.current_run_id is None:
        raise RuntimeError(
            "cannot start isolated_systemd Kanban worker: claimed task has no current_run_id"
        )
    if not task.claim_lock:
        raise RuntimeError(
            "cannot start isolated_systemd Kanban worker: claimed task has no claim_lock"
        )

    available, reason = systemd_user_service_available(env)
    if not available:
        raise RestartSafeScopeUnavailable(
            f"cannot create isolated systemd service for Kanban worker: {reason}"
        )

    run_bin = _command('systemd-run')
    if not run_bin:
        raise RestartSafeScopeUnavailable(
            "cannot create isolated systemd service for Kanban worker: systemd-run disappeared"
        )

    resolved_db = Path(board_db).expanduser().resolve()
    resolved_profile = Path(profile_home).expanduser().resolve()
    unit_name = isolated_worker_unit_name(resolved_db, task.id, int(task.current_run_id))
    root_dir = attempt_root_dir(resolved_db, task.id, int(task.current_run_id))
    # Plan, validate, and materialize outside any board write lock; never copies skills/plugins.
    plan = prepare_attempt_home_plan(root_dir / "home", resolved_profile)
    attempt_home = plan.attempt_home
    launch_path = root_dir / "launch.json"

    boot_id = current_instantiation_epoch()
    if not boot_id:
        raise RestartSafeScopeUnavailable('cannot verify isolated worker boot identity')
    from hermes_cli.kanban_worker_env import isolated_owner_env
    worker_env = isolated_owner_env(env, resolved_profile)
    worker_env['PYTHONPATH'] = env['PYTHONPATH']
    worker_env['HERMES_HOME'] = str(attempt_home)
    board_label = board or env['HERMES_KANBAN_BOARD'] or 'default'
    with os.fdopen(os.open(launch_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as launch_file:
        json.dump({
            'environment': worker_env,
            'claim': {'board': env['HERMES_KANBAN_BOARD'], 'task_id': task.id,
                      'run_id': int(task.current_run_id), 'claim_lock': task.claim_lock},
            'board_db': str(resolved_db), 'profile_home': str(resolved_profile),
            'attempt_home': str(attempt_home), 'unit_name': unit_name, 'boot_id': boot_id,
            'readonly_mounts': [proj.to_manifest() for proj in plan.readonly_projections],
        }, launch_file)

    resolved_log = Path(log_path).expanduser().resolve()
    working_dir = workspace if workspace and os.path.isdir(workspace) else str(attempt_home)
    description = f"Hermes Kanban isolated worker {board_label}/{task.id}/run-{int(task.current_run_id)}"

    ro_bind_args: list[str] = []
    for proj in plan.readonly_projections:
        ro_bind_args.extend(["--property", f"BindReadOnlyPaths={proj.bind_spec}"])

    no_expand = ["--expand-environment=no"] if _PROBE_NO_EXPAND else []
    argv = [
        run_bin, "--user", "--quiet",
        "--unit", unit_name,
        "--service-type=exec",
        "--collect",
        "--working-directory", working_dir,
        "--property", f"Description={_systemd_escape_specifiers(description)}",
        "--property", f"StandardOutput=append:{_systemd_escape_specifiers(str(resolved_log))}",
        "--property", f"StandardError=append:{_systemd_escape_specifiers(str(resolved_log))}",
        "--property", "KillMode=control-group",
        "--property", "TimeoutStopSec=15",
        "--property", "MemoryAccounting=yes",
        "--property", f"MemoryMax={_worker_memory_max_bytes()}",
        *ro_bind_args,
        *no_expand,
        "--", sys.executable, "-IS", str(Path(__file__).with_name('kanban_isolation') / 'clean_exec.py'),
        str(launch_path), sys.executable, "-P", "-m", "hermes_cli.kanban_isolated_worker", str(launch_path),
    ]
    intent = {'worker_plane': WORKER_PLANE_ISOLATED_SYSTEMD, 'systemd_unit': unit_name, 'boot_id': boot_id}
    _record_launch_intent(resolved_db, task, intent)
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            env=_bus_env(worker_env), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LaunchUnconfirmed('systemd launch timed out; holding claim') from exc
    if proc.returncode != 0:
        if isolated_unit_state(intent) != UnitState.GONE:
            raise LaunchUnconfirmed('cannot confirm isolated launch failed; holding claim')
        try:
            launch_path.unlink(missing_ok=True)
        except OSError:
            pass
        detail = (proc.stderr or proc.stdout or f"rc={proc.returncode}").strip()
        if any(
            tok in detail.lower()
            for tok in ("bus", "dbus", "failed to connect", "systemd", "mount", "namespace", "bind", "operation not permitted")
        ):
            reset_systemd_service_probe_cache()
            raise RestartSafeScopeUnavailable(
                f"cannot create isolated systemd service for Kanban worker: {detail}"
            )
        raise RuntimeError(f"failed to start isolated Kanban worker unit {unit_name}: {detail}")

    props = inspect_systemd_unit(unit_name, env=worker_env)
    raw_pid = props.get("MainPID") or props.get("ExecMainPID") or "0"
    try:
        pid = int(raw_pid)
    except ValueError:
        pid = 0
    invocation_id = props.get("InvocationID") or ""
    started_at = (_process_fingerprint(pid) if pid > 0 else None) or UNVERIFIED_WORKER_FINGERPRINT
    return IsolatedLaunchReceipt(
        pid=pid,
        started_at=started_at,
        unit_name=unit_name,
        invocation_id=invocation_id,
        boot_id=boot_id,
        attempt_home=str(attempt_home),
        profile_home=str(resolved_profile),
        readonly_assets=tuple(proj.name for proj in plan.readonly_projections),
    )


def run_worker_plane_identity(
    conn,
    task_id: str,
    run_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Return the isolated systemd identity recorded for *task_id*'s active/given run, else None."""
    if conn is None or not task_id:
        return None
    if run_id is None:
        row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["current_run_id"] is None:
            return None
        run_id = int(row["current_run_id"])
    rows = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind IN ('unit_requested', 'spawned', 'worker_registered', 'owner_admitted', 'worker_bound') "
        "ORDER BY id",
        (task_id, int(run_id)),
    ).fetchall()
    merged: dict[str, Any] = {}
    isolated = False
    conflicts = set()
    for ev in rows:
        isolated |= ev['kind'] == 'unit_requested'
        try:
            payload = json.loads(ev["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        identified = payload.get('worker_plane') == WORKER_PLANE_ISOLATED_SYSTEMD
        if not (identified or 'systemd_unit' in payload):
            continue
        isolated = True
        keys = ('worker_plane', 'systemd_unit', 'invocation_id', 'boot_id', 'attempt_home', 'profile_home')
        if ev['kind'] in ('spawned', 'worker_registered'):
            keys += ('pid', 'started_at')
        for key in keys:
            if value := payload.get(key):
                if key in merged and merged[key] != value:
                    conflicts.add(key)
                merged[key] = value
    if not isolated:
        return None
    for key in conflicts:
        merged.pop(key, None)
    from hermes_cli.kanban_db_connect import _main_db_file
    database = _main_db_file(conn)
    if not database or merged.get('systemd_unit') != isolated_worker_unit_name(database, task_id, run_id):
        merged.pop('systemd_unit', None)
    merged["task_id"] = task_id
    merged["run_id"] = int(run_id)
    return merged


def previous_run(conn, task_id):
    return conn.execute('SELECT max(id) FROM task_runs WHERE task_id=?', (task_id,)).fetchone()[0]


def previous_unit_may_run(conn, task_id, run_id):
    if run_id is None:
        return False
    identity = run_worker_plane_identity(conn, task_id, run_id)
    if identity is None:
        return False
    from hermes_cli.kanban_db import _host_prefix
    from hermes_cli.kanban_db_dispatch import _worker_alive
    row = conn.execute('SELECT claim_lock, worker_pid, worker_started_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
    if not (row[0] or '').startswith(_host_prefix()) or _worker_alive(row[1], row[2]):
        return True
    return isolated_unit_state(identity) != UnitState.GONE


class UnitState(Enum):
    UNKNOWN = 'unknown'
    LIVE = 'live'
    GONE = 'gone'


def isolated_unit_state(identity: Mapping[str, Any]) -> UnitState:
    """Only positive manager evidence proves absence; an RPC failure proves nothing."""
    from gateway.drain_control import current_instantiation_epoch
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    name = identity.get('systemd_unit')
    if (not _IS_LINUX or not isinstance(name, str) or not name.startswith('hermes-kanban-w-')
            or identity.get('worker_plane') != WORKER_PLANE_ISOLATED_SYSTEMD):
        return UnitState.UNKNOWN
    props = inspect_systemd_unit(name)
    if props.get('Id') != name:
        return UnitState.UNKNOWN
    if (props.get('LoadState') in {'not-found', 'loaded'} and props.get('ActiveState') in {'inactive', 'failed'}
            and props.get('MainPID') == '0' and props.get('ControlGroup') == ''):
        return UnitState.GONE
    if (props.get('LoadState') != 'loaded' or props.get('ActiveState') not in _ACTIVE_UNIT_STATES
            or not identity.get('boot_id') or identity['boot_id'] != current_instantiation_epoch()
            or not identity.get('invocation_id') or identity['invocation_id'] != props.get('InvocationID')):
        return UnitState.UNKNOWN
    pid = identity.get('pid')
    group = props.get('ControlGroup')
    if (not pid or str(pid) != props.get('MainPID') or not group or group.split('/')[-1] != name
            or read_current_cgroup_path(pid) != group or not identity.get('started_at')
            or _process_fingerprint(pid) != identity['started_at']):
        return UnitState.UNKNOWN
    return UnitState.LIVE


def stop_isolated_worker_unit(identity: Mapping[str, Any], *, timeout: float = 20.0) -> dict[str, Any]:
    state = isolated_unit_state(identity)
    attempted = False
    if state == UnitState.LIVE and (ctl := _command('systemctl')):
        attempted = True
        try:
            subprocess.run([ctl, '--user', 'stop', identity['systemd_unit']], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout, env=_bus_env(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            state = UnitState.UNKNOWN
        else:
            state = isolated_unit_state(identity)
    return {'systemd_unit': identity.get('systemd_unit'), 'invocation_id': identity.get('invocation_id'),
            'unit_stop_attempted': attempted, 'unit_state': state.value, 'terminated': state == UnitState.GONE}


def kanban_attempt_session_summary(conn, task_id: str, run_id: Optional[int] = None) -> Optional[dict[str, Any]]:
    """Read-only visibility helper: return session + receipt + unit metadata for one attempt."""
    if conn is None or not task_id:
        return None
    if run_id is None:
        row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row["current_run_id"] is None:
            rrow = conn.execute(
                "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if rrow is None:
                return None
            run_id = int(rrow["id"])
        else:
            run_id = int(row["current_run_id"])
    rows = conn.execute(
        "SELECT kind, payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind IN ('spawned', 'owner_admitted', 'worker_bound', 'worker_result') "
        "ORDER BY id ASC",
        (task_id, int(run_id)),
    ).fetchall()
    summary: dict[str, Any] = {"task_id": task_id, "run_id": int(run_id)}
    for ev in rows:
        try:
            payload = json.loads(ev["payload"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        kind = ev["kind"]
        if kind == "owner_admitted":
            summary["session_id"] = payload.get("session_id")
            summary["owner_db"] = payload.get("db")
            summary["request_id"] = payload.get("request_id")
            summary["owner_pid"] = payload.get("pid")
            summary["owner_birth"] = payload.get("birth")
        elif kind == "worker_bound":
            summary["bound_pid"] = payload.get("pid")
            summary["bound_started_at"] = payload.get("started_at")
            if payload.get("cgroup"):
                summary["bound_cgroup"] = payload.get("cgroup")
        elif kind == "worker_result":
            summary["exit_code"] = payload.get("exit_code")
        for key in ("worker_plane", "systemd_unit", "invocation_id", "boot_id", "attempt_home", "profile_home"):
            if payload.get(key) and key not in summary:
                summary[key] = payload[key]
    return summary if len(summary) > 2 else None
