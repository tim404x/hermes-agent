#!/usr/bin/env python3
"""Runnable staging integration harness for Kanban worker-plane restart isolation.

Spins up ONLY private, uniquely named user systemd units against a scratch temp HOME,
scratch kanban.db, and local deterministic HTTP model server (zero real provider
credentials, zero production touches, zero system-wide restarts):

  1. Starts a private staging gateway unit (`hermes-staging-gw-<id>.service`) running
     the real `hermes gateway run` with `kanban.dispatch_in_gateway: true` and the
     requested `kanban.worker_plane` (`isolated_systemd` or `profile_owner`).
  2. Creates a real Kanban card for a scratch named profile and lets the gateway's normal
     embedded dispatcher claim and launch it.
  3. The deterministic model server issues a real `terminal` tool call that appends ONE
     side-effect marker line to `<workspace>/side_effect.log`, then pauses on a barrier
     before returning `kanban_complete`.
  4. Captures BEFORE-restart markers: old gateway PID/cgroup, card `run_id`,
     `owner_admitted`, `worker_bound` PID/fingerprint/cgroup, worker unit state, and
     side-effect count == 1.
  5. Restarts ONLY the private staging gateway unit (`systemctl --user restart <gw_unit>`),
     waits for the new gateway PID to reach `ready`, and lets it tick the board.
  6. Captures DURING/AFTER-restart markers, releases the model barrier, waits for card
     completion, verifies same attempt / same bound agent fingerprint+cgroup / one tool
     side effect / persisted receipt in attempt-private `state.db`, and writes a full
     JSON receipt.
  7. Stops and removes ONLY its own staging unit(s) in `finally`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_until(pred, timeout: float, what: str, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = pred()
        if val:
            return val
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.1f}s waiting for {what}")


def _proc_cgroup(pid: Optional[int]) -> Optional[str]:
    if not pid or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2].strip():
            return parts[2].strip()
    return None


def _live_birth(pid: int) -> str | None:
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        init_ticks = Path('/proc/1/stat').read_text().rsplit(')', 1)[1].split()[19]
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return f'{boot}:{init_ticks}|{fields[19]}' if fields[0] != 'Z' else None
    except (OSError, IndexError):
        return None


def _systemctl_show(unit: str) -> dict[str, str]:
    proc = subprocess.run(
        [
            "systemctl", "--user", "show",
            "--property=Id,LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup",
            unit,
        ],
        capture_output=True, text=True, check=False, timeout=10, env=_manager_env(),
    )
    out: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _manager_env():
    runtime = f'/run/user/{os.getuid()}'
    return {'PATH': os.defpath, 'XDG_RUNTIME_DIR': runtime, 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus'}


def _inactive(unit):
    props = _systemctl_show(unit)
    return (props.get('Id') == unit and props.get('LoadState') in {'not-found', 'loaded'}
            and props.get('ActiveState') == 'inactive' and props.get('MainPID') == '0' and props.get('ControlGroup') == '')


def _has_env_name(pid, name):
    prefix = name.encode() + b'='
    return any(entry.startswith(prefix) for entry in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'))


class _DeterministicModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        msgs = body.get("messages") or []
        srv = self.server

        with srv.lock:
            srv.call_count += 1
            call_no = srv.call_count

        has_tool_result = any(m.get("role") == "tool" for m in msgs)
        complete_ids = {call['id'] for msg in msgs for call in msg.get('tool_calls', [])
                        if call.get('function', {}).get('name') == 'kanban_complete'}
        has_complete_result = any(msg.get('role') == 'tool' and msg.get('tool_call_id') in complete_ids
                                  for msg in msgs)
        has_side_effect_result = any(
            m.get("role") == "tool" and "STAGING_EFFECT_OK" in str(m.get("content", ""))
            for m in msgs
        )

        if has_complete_result:
            message = {"role": "assistant", "content": "Staging card completed cleanly."}
        elif has_side_effect_result:
            # Side effect already happened! Hold here across the gateway restart.
            srv.after_tool_reached.set()
            srv.release_complete.wait(timeout=180)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_complete_{call_no}",
                    "type": "function",
                    "function": {
                        "name": "kanban_complete",
                        "arguments": json.dumps({
                            "summary": "Staging isolated worker survived gateway restart",
                        }),
                    },
                }],
            }
        elif not has_tool_result:
            with srv.lock:
                srv.first_turn_calls += 1
            effect_cmd = (
                f"printf 'EFFECT pid=%s ts=%s\\n' \"$$\" \"$(date +%s)\" >> {srv.side_effect_file} "
                f"&& echo STAGING_EFFECT_OK"
            )
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_effect_{call_no}",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": json.dumps({"command": effect_cmd, "timeout": 15}),
                    },
                }],
            }
        else:
            message = {"role": "assistant", "content": "Unexpected state."}

        choice = {
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
        }
        frame = {
            "id": f"staging-{call_no}",
            "model": "staging-deterministic",
            "choices": [choice],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        if body.get("stream"):
            choice["delta"] = choice.pop("message")
            for idx, tc in enumerate(choice["delta"].get("tool_calls") or []):
                tc["index"] = idx
            payload = ("data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n").encode("utf-8")
            ctype = "text/event-stream"
        else:
            payload = json.dumps(frame).encode("utf-8")
            ctype = "application/json"

        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def _query_db(db_path: Path, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _latest_event(db_path: Path, task_id: str, kind: str) -> Optional[dict[str, Any]]:
    rows = _query_db(
        db_path,
        "SELECT id, run_id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    )
    if not rows:
        return None
    row = rows[0]
    try:
        row["payload"] = json.loads(row["payload"] or "{}")
    except (ValueError, TypeError):
        row["payload"] = {}
    return row


def _read_gateway_identify(hermes_home: Path) -> Optional[dict[str, Any]]:
    from hermes_cli.gateway_runtime_discovery import query_identify

    try:
        return query_identify(hermes_home, timeout=2.0)
    except Exception:
        return None


def _start_gateway(unit_name, python_exe, environment, log_path):
    manifest = log_path.parent / 'gateway-env.json'
    manifest.write_text(json.dumps(environment))
    subprocess.run([
        'systemd-run', '--user', '--quiet', '--collect', '--service-type=exec', '--unit', unit_name,
        '--working-directory', str(REPO_ROOT), '-p', 'TimeoutStopSec=15',
        '-p', f'StandardOutput=append:{log_path}', '-p', f'StandardError=append:{log_path}',
        '--setenv=B4_MANAGER_ONLY=synthetic', '--', python_exe, '-IS',
        str(REPO_ROOT / 'hermes_cli/kanban_isolation/clean_exec.py'), str(manifest), python_exe,
        '-m', 'hermes_cli.main', 'gateway', 'run',
    ], check=True, timeout=20, env=_manager_env())


def run_staging(*, worker_plane: str, python_exe: str, keep_scratch: bool = False) -> dict[str, Any]:
    run_tag = uuid.uuid4().hex[:10]
    sys.path.insert(0, str(REPO_ROOT))
    from hermes_cli.kanban_worker_plane import isolated_worker_unit_name
    scratch_base = Path(f'/run/user/{os.getuid()}')
    staging_root = Path(tempfile.mkdtemp(prefix='b4-', dir=scratch_base))
    staging_home = staging_root / "home"
    hermes_home = staging_home / ".hermes"
    hermes_home.mkdir(parents=True, mode=0o700)
    gw_log = staging_root / "staging-gateway.log"
    side_effect_file = staging_root / "side_effect.log"

    gw_unit = f"hermes-staging-gw-{run_tag}.service"
    task_id = None
    board_db = hermes_home / 'kanban.db'
    gateway_started = False

    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicModelHandler)
    server.lock = threading.Lock()
    server.call_count = 0
    server.first_turn_calls = 0
    server.side_effect_file = str(side_effect_file)
    server.after_tool_reached = threading.Event()
    server.release_complete = threading.Event()
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"

    receipt: dict[str, Any] = {
        "run_tag": run_tag,
        "worker_plane": worker_plane,
        "staging_root": str(staging_root),
        "hermes_home": str(hermes_home),
        "gateway_unit": gw_unit,
        "model_base_url": base_url,
        "markers": {},
        "passed": False,
    }

    try:
        (hermes_home / "config.yaml").write_text(
            "model:\n"
            "  provider: custom\n"
            f"  base_url: {base_url}\n"
            "  default: staging-deterministic\n"
            "  context_length: 128000\n"
            "agent:\n"
            "  api_max_retries: 1\n"
            "updates:\n"
            "  check: false\n"
            "auxiliary:\n"
            "  title_generation:\n"
            "    enabled: false\n"
            "gateway:\n"
            "  standalone: true\n"
            "kanban:\n"
            "  dispatch_in_gateway: true\n"
            "  notify_in_gateway: false\n"
            "  auto_decompose: false\n"
            "  dispatch_interval_seconds: 1\n"
            f"  worker_plane: {worker_plane}\n",
            encoding="utf-8",
        )
        (hermes_home / ".env").write_text("B4_OTHER_PROFILE_ONLY=synthetic\n", encoding="utf-8")
        (hermes_home / ".env").chmod(0o600)
        (hermes_home / "models_dev_cache.json").write_text(
            json.dumps({"hermes-test-offline": {"id": "hermes-test-offline", "env": [], "models": {}}}),
            encoding="utf-8",
        )

        # Initialize board and create one real card via CLI (scratch HOME/HERMES_HOME).
        assignee = 'b4-worker' if worker_plane == 'isolated_systemd' else 'default'
        profile = hermes_home / 'profiles' / assignee if assignee != 'default' else hermes_home
        if profile != hermes_home:
            profile.mkdir(mode=0o700, parents=True)
            for name in ('config.yaml', 'models_dev_cache.json'):
                shutil.copyfile(hermes_home / name, profile / name)
        (profile / '.env').write_text('OPENAI_API_KEY=local-fixture-only\nHTTPS_PROXY=http://127.0.0.1:1\n'
                                      'HTTP_PROXY=http://127.0.0.1:1\nNO_PROXY=127.0.0.1,localhost,::1\n')
        runtime = staging_root / 'r'
        runtime.mkdir(mode=0o700)
        cli_env = {
            'PATH': f'{Path(python_exe).parent}:/usr/bin:/bin', 'LANG': 'C.UTF-8',
            'XDG_RUNTIME_DIR': str(runtime), 'DBUS_SESSION_BUS_ADDRESS': f'unix:path={scratch_base}/bus',
            'HERMES_GATEWAY_LOCK_DIR': str(runtime / 'locks'), 'HERMES_RUNTIME_DIR': str(runtime / 'pm'),
            'TMPDIR': str(runtime), 'HERMES_DISABLE_LAZY_INSTALLS': '1', 'GIT_ALLOW_PROTOCOL': 'file',
            'HTTPS_PROXY': 'http://127.0.0.1:1', 'HTTP_PROXY': 'http://127.0.0.1:1',
            'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1,localhost,::1',
            'HERMES_SUPERVISED_CHILD': '1', 'HERMES_KANBAN_CRASH_GRACE_SECONDS': '0',
            "HOME": str(staging_home),
            'HERMES_REAL_HOME': str(staging_home), 'PYTHONDONTWRITEBYTECODE': '1',
            "HERMES_HOME": str(hermes_home),
            "PYTHONPATH": str(REPO_ROOT),
            "NO_COLOR": "1",
        }
        subprocess.run(
            [python_exe, "-m", "hermes_cli.main", "kanban", "init"],
            cwd=str(REPO_ROOT), env=cli_env, check=True, capture_output=True, text=True, timeout=60,
        )
        created_proc = subprocess.run(
            [
                python_exe, "-m", "hermes_cli.main", "kanban", "create",
                "staging restart isolation card",
                "--body", "Run one terminal tool effect then call kanban_complete.",
                "--assignee", assignee, "--json",
            ],
            cwd=str(REPO_ROOT), env=cli_env, check=True, capture_output=True, text=True, timeout=60,
        )
        created_json = json.loads(created_proc.stdout[created_proc.stdout.index("{"):])
        task_id = created_json["id"]
        board_db = hermes_home / "kanban.db"
        receipt["task_id"] = task_id
        receipt["board_db"] = str(board_db)

        # Install & start ONLY our private staging gateway user unit.
        _start_gateway(gw_unit, python_exe, cli_env, gw_log)
        gateway_started = True

        old_ident = _wait_until(
            lambda: (d := _read_gateway_identify(hermes_home)) and d.get("state") == "ready" and d,
            timeout=90, what="staging gateway ready",
        )
        old_gw_props = _systemctl_show(gw_unit)
        old_gw_pid = int(old_ident["pid"])
        old_verified = (old_gw_props.get('MainPID') == str(old_gw_pid)
                        and old_gw_props.get('ControlGroup') == _proc_cgroup(old_gw_pid))
        receipt["old_gateway"] = {
            "pid": old_gw_pid,
            "unit": gw_unit,
            "invocation_id": old_gw_props.get("InvocationID"),
            "cgroup": old_gw_props.get("ControlGroup") or _proc_cgroup(old_gw_pid),
        }

        # Wait for the real card to be dispatched, bind its agent worker, and execute its tool effect!
        def reached_barrier():
            task = _query_db(board_db, 'SELECT status FROM tasks WHERE id=?', (task_id,))[0]
            if task['status'] in ('blocked', 'done'):
                raise RuntimeError(f"card {task['status']} before terminal barrier")
            return server.after_tool_reached.is_set()

        _wait_until(
            reached_barrier,
            timeout=120, what="worker to execute tool effect and pause before kanban_complete",
        )
        before_task = _query_db(board_db, "SELECT * FROM tasks WHERE id = ?", (task_id,))[0]
        before_admitted = _latest_event(board_db, task_id, "owner_admitted")
        before_bound = _latest_event(board_db, task_id, "worker_bound")
        before_spawned = _latest_event(board_db, task_id, "spawned")
        assert before_bound is not None, "missing worker_bound event before restart"
        bound_pid_before = int(before_bound["payload"]["pid"])
        bound_fp_before = before_bound["payload"].get("started_at")
        bound_cgroup_before = _proc_cgroup(bound_pid_before)
        worker_unit = (before_admitted or {}).get("payload", {}).get("systemd_unit") or (before_spawned or {}).get("payload", {}).get("systemd_unit")
        if worker_plane == 'isolated_systemd':
            assert worker_unit == isolated_worker_unit_name(board_db, task_id, before_task['current_run_id'])
        before_birth = _live_birth(bound_pid_before)
        owner_pid = (before_admitted or {}).get('payload', {}).get('pid')
        isolated_environment = bool(owner_pid) and all(
            not _has_env_name(pid, name) for pid in (owner_pid, bound_pid_before)
            for name in ('B4_MANAGER_ONLY', 'B4_OTHER_PROFILE_ONLY'))
        gateway_clean = not _has_env_name(old_gw_pid, 'B4_MANAGER_ONLY')
        side_effects_before = side_effect_file.read_text(encoding="utf-8").splitlines() if side_effect_file.exists() else []

        receipt["markers"]["before_restart"] = {
            "ts": time.time(),
            "gateway_pid": old_gw_pid,
            "gateway_cgroup": receipt["old_gateway"]["cgroup"],
            "task_status": before_task["status"],
            "run_id": before_task["current_run_id"],
            "worker_pid": before_task["worker_pid"],
            "worker_started_at": before_task["worker_started_at"],
            "worker_unit": worker_unit,
            "worker_unit_state": _systemctl_show(worker_unit) if worker_unit else None,
            "owner_admitted": (before_admitted or {}).get("payload"),
            "bound_pid": bound_pid_before,
            "bound_fingerprint": bound_fp_before,
            "bound_cgroup": bound_cgroup_before,
            "live_birth": before_birth,
            "side_effect_count": len(side_effects_before),
        }

        # Restart ONLY the private staging gateway unit!
        receipt["markers"]["during_restart"] = {"ts": time.time(), "action": f"systemctl --user restart {gw_unit}"}
        subprocess.run(["systemctl", "--user", "restart", gw_unit], check=True, timeout=45, env=_manager_env())
        receipt['markers']['during_restart'].update(manager_returned=time.time(), live_birth=_live_birth(bound_pid_before))

        new_ident = _wait_until(
            lambda: (
                (d := _read_gateway_identify(hermes_home))
                and d.get("state") == "ready"
                and int(d.get("pid", 0)) != old_gw_pid
                and d
            ),
            timeout=90, what="restarted staging gateway ready with new PID",
        )
        new_gw_props = _systemctl_show(gw_unit)
        new_gw_pid = int(new_ident["pid"])
        receipt["new_gateway"] = {
            "pid": new_gw_pid,
            "unit": gw_unit,
            "invocation_id": new_gw_props.get("InvocationID"),
            "cgroup": new_gw_props.get("ControlGroup") or _proc_cgroup(new_gw_pid),
        }

        # Let the restarted gateway run at least 2 dispatcher ticks (interval=1s) while worker is paused!
        time.sleep(3.5)

        after_restart_task = _query_db(board_db, "SELECT * FROM tasks WHERE id = ?", (task_id,))[0]
        after_restart_runs = _query_db(board_db, "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,))
        after_birth = _live_birth(bound_pid_before)
        bound_cgroup_after_restart = _proc_cgroup(bound_pid_before)
        side_effects_after_restart = side_effect_file.read_text(encoding="utf-8").splitlines() if side_effect_file.exists() else []

        receipt["markers"]["after_restart_before_release"] = {
            "ts": time.time(),
            "old_gateway_pid": old_gw_pid,
            "new_gateway_pid": new_gw_pid,
            "task_status": after_restart_task["status"],
            "run_id": after_restart_task["current_run_id"],
            "run_count": len(after_restart_runs),
            "worker_unit_state": _systemctl_show(worker_unit) if worker_unit else None,
            "bound_pid": bound_pid_before,
            "live_birth": after_birth,
            "bound_cgroup": bound_cgroup_after_restart,
            "side_effect_count": len(side_effects_after_restart),
        }

        # Release the model barrier so the surviving worker completes!
        server.release_complete.set()

        if worker_plane == "isolated_systemd":
            _wait_until(
                lambda: _query_db(board_db, "SELECT status FROM tasks WHERE id = ?", (task_id,))[0]["status"] == "done",
                timeout=90, what="card to reach done after gateway restart",
            )
            if worker_unit:
                _wait_until(
                    lambda: _inactive(worker_unit),
                    timeout=60, what="isolated worker unit to exit cleanly",
                )
        else:
            # In profile_owner mode, wait briefly to observe the failure/crash/second attempt.
            time.sleep(4.0)

        final_task = _query_db(board_db, "SELECT * FROM tasks WHERE id = ?", (task_id,))[0]
        final_runs = _query_db(board_db, "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,))
        final_events = _query_db(
            board_db,
            "SELECT id, run_id, kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        for ev in final_events:
            with contextlib.suppress(ValueError, TypeError):
                ev["payload"] = json.loads(ev["payload"] or "{}")

        final_side_effects = side_effect_file.read_text(encoding="utf-8").splitlines() if side_effect_file.exists() else []
        attempt_db_path = Path((before_admitted or {}).get("payload", {}).get("db", ""))
        attempt_session_id = (before_admitted or {}).get("payload", {}).get("session_id")
        attempt_receipt_rows = []
        attempt_session_rows = []
        shared_session_rows = []
        if attempt_db_path.is_file() and attempt_session_id:
            attempt_receipt_rows = _query_db(
                attempt_db_path,
                "SELECT admission_id, target_session_id AS session_id, request_id, status, outcome "
                "FROM session_admissions WHERE target_session_id = ?",
                (attempt_session_id,),
            )
            attempt_session_rows = _query_db(
                attempt_db_path,
                "SELECT id, source, model FROM sessions WHERE id = ?",
                (attempt_session_id,),
            )
            shared_db = hermes_home / "state.db"
            if shared_db.is_file() and shared_db.resolve() != attempt_db_path.resolve():
                shared_session_rows = _query_db(
                    shared_db,
                    "SELECT id, source FROM sessions WHERE id = ?",
                    (attempt_session_id,),
                )

        receipt["final"] = {
            "task_status": final_task["status"],
            "run_count": len(final_runs),
            "runs": [{key: row[key] for key in ('id', 'task_id', 'profile', 'status', 'outcome', 'worker_pid', 'worker_started_at')} for row in final_runs],
            "event_kinds": [e["kind"] for e in final_events],
            "side_effect_count": len(final_side_effects),
            "side_effects": final_side_effects,
            "model_first_turn_calls": server.first_turn_calls,
            "model_total_calls": server.call_count,
            "attempt_db": str(attempt_db_path),
            "attempt_session_id": attempt_session_id,
            "attempt_admission_rows": attempt_receipt_rows,
            "attempt_session_rows": attempt_session_rows,
            "shared_db_has_attempt_session": bool(shared_session_rows),
        }

        same_attempt = (
            len(final_runs) == 1
            and final_runs[0]["id"] == before_task["current_run_id"]
            and final_runs[0]["outcome"] == "completed"
        )
        same_bound_agent = (
            before_birth is not None and before_birth == after_birth == bound_fp_before
            and bound_cgroup_after_restart == bound_cgroup_before
            and bool(worker_unit)
            and worker_unit in (bound_cgroup_before or "")
            and (bound_cgroup_before or "") != (receipt["old_gateway"]["cgroup"] or "")
        )
        one_side_effect = len(final_side_effects) == 1 and server.first_turn_calls == 1
        persisted_receipt = (
            len(attempt_receipt_rows) == 1
            and attempt_receipt_rows[0]["status"] == "terminal" and attempt_receipt_rows[0]['outcome'] == 'completed'
            and len(attempt_session_rows) == 1
            and attempt_session_rows[0]["source"] == "kanban"
            and not shared_session_rows and (profile == hermes_home or not (profile / 'state.db').exists())
        )
        first = receipt['markers']['before_restart']
        second = receipt['markers']['after_restart_before_release']
        first_unit = first['worker_unit_state'] or {}
        second_unit = second['worker_unit_state'] or {}
        receipt["assertions"] = {
            'private_gateway_identity': old_verified and new_gw_props.get('MainPID') == str(new_gw_pid)
                and new_gw_props.get('ControlGroup') == _proc_cgroup(new_gw_pid)
                and bool(old_gw_props.get('InvocationID')) and bool(new_gw_props.get('InvocationID'))
                and old_gw_props['InvocationID'] != new_gw_props['InvocationID'],
            'worker_present_at_restart_return': bool(before_birth) and receipt['markers']['during_restart']['live_birth'] == before_birth,
            'isolated_environment': gateway_clean and not _has_env_name(new_gw_pid, 'B4_MANAGER_ONLY') and isolated_environment,
            'single_tool_sequence': server.call_count == 3 and sum(event['kind'] == 'worker_result' and event['payload'].get('exit_code') == 0 for event in final_events) == 1,
            'named_assignee': before_task['assignee'] == assignee and (before_admitted or {}).get('payload', {}).get('profile_home') == str(profile),
            'same_unit_invocation': bool(first_unit.get('InvocationID')) and first_unit['InvocationID'] == second_unit.get('InvocationID') == (before_admitted or {}).get('payload', {}).get('invocation_id'),
            'same_active_cgroup': first_unit.get('ActiveState') == second_unit.get('ActiveState') == 'active' and first_unit.get('ControlGroup') == second_unit.get('ControlGroup') == bound_cgroup_before,
            'one_effect_while_paused': len(side_effects_before) == len(side_effects_after_restart) == 1,
            'same_claim_while_paused': first['run_id'] == second['run_id'] and second['run_count'] == 1 and second['task_status'] == 'running',
            "gateway_restarted_new_pid": new_gw_pid != old_gw_pid,
            "same_attempt": same_attempt,
            "same_bound_agent_fingerprint_and_cgroup": same_bound_agent,
            "final_completed": final_task["status"] == "done",
            "one_side_effect": one_side_effect,
            "persisted_attempt_receipt_no_shared_writer": persisted_receipt,
        }
        receipt["passed"] = all(receipt["assertions"].values())
        return receipt
    except Exception as exc:
        receipt['error'] = str(exc)
        return receipt
    finally:
        server.release_complete.set()
        with contextlib.suppress(Exception):
            server.shutdown()
            server.server_close()
        # Stop ONLY our own private staging units.
        cleanup = {}
        owned_units = [gw_unit] if gateway_started else []
        if task_id and worker_plane == 'isolated_systemd':
            for row in _query_db(board_db, 'SELECT id FROM task_runs WHERE task_id=?', (task_id,)):
                owned_units.append(isolated_worker_unit_name(board_db, task_id, row['id']))
        for unit in owned_units:
            cleanup[unit] = False
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(['systemctl', '--user', 'stop', unit], capture_output=True, timeout=20, env=_manager_env())
                cleanup[unit] = _inactive(unit)
        receipt['verified_cleanup'] = cleanup
        receipt['passed'] = receipt['passed'] and all(cleanup.values())
        if not keep_scratch and all(cleanup.values()):
            with contextlib.suppress(OSError):
                shutil.rmtree(staging_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("isolated_systemd", "profile_owner"),
        default="isolated_systemd",
        help="kanban.worker_plane mode to exercise",
    )
    parser.add_argument("--python", default=sys.executable, help="Python interpreter for staging gateway/CLI")
    parser.add_argument("--receipt", default="", help="Optional path to write JSON receipt")
    parser.add_argument("--keep-scratch", action="store_true", help="Keep scratch directory for inspection")
    args = parser.parse_args()

    receipt = run_staging(worker_plane=args.mode, python_exe=args.python, keep_scratch=args.keep_scratch)
    rendered = json.dumps(receipt, indent=2, default=str)
    if args.receipt:
        Path(args.receipt).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
