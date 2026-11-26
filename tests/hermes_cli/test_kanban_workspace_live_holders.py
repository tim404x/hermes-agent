"""Regression tests for live foreign-process workspace cleanup guards (card t_d2993a3a).

Completion, archival, deferred parent cleanup, and ``kanban gc`` must never
``rmtree`` a scratch workspace (or remove a linked worktree) while a foreign
live process (e.g. a detached ``systemd-run`` transient unit or background job)
still holds references to it. Deferred cleanups are retried by the dispatcher
reclaim phase and by ``kanban gc`` until the holders exit.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_ops
from tests.waiting import wait_until


def _systemd_user_scope_available() -> bool:
    if not shutil.which("systemd-run"):
        return False
    try:
        res = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _stop_holder(proc: subprocess.Popen, unit: Optional[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if unit and shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "stop", f"{unit}.scope"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    wait_until(
        lambda: not Path(f"/proc/{proc.pid}").exists(),
        timeout=10.0,
        what=f"/proc/{proc.pid} to exit",
    )


def _wait_holder_cwd(proc: subprocess.Popen, ws: Path, unit: Optional[str] = None) -> None:
    ws_real = os.path.realpath(ws)

    def _ready() -> bool:
        if proc.poll() is not None:
            raise AssertionError(f"holder process exited prematurely with rc={proc.returncode}")
        try:
            cwd = os.readlink(f"/proc/{proc.pid}/cwd")
        except OSError:
            return False
        if not (cwd == ws_real or cwd.startswith(ws_real + os.sep)):
            return False
        if unit:
            try:
                cg = Path(f"/proc/{proc.pid}/cgroup").read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return unit in cg
        return True

    wait_until(_ready, timeout=10.0, what=f"holder pid {proc.pid} cwd in {ws}")


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_TASK",
    ):
        monkeypatch.delenv(var, raising=False)
    retry_map = getattr(kbw, "_LIVE_CLEANUP_LAST_RETRY", None)
    if isinstance(retry_map, dict):
        retry_map.clear()
    return tmp_path


@pytest.fixture
def holder_spawner(monkeypatch):
    """Spawn holder processes and guarantee cleanup on teardown."""
    spawned: list[tuple[subprocess.Popen, Optional[str]]] = []

    def spawn_foreign(ws: Path, *, use_systemd: bool = True) -> tuple[subprocess.Popen, Optional[str]]:
        if use_systemd and _systemd_user_scope_available():
            unit = f"kbtest-hold-{uuid.uuid4().hex[:10]}"
            proc = subprocess.Popen(
                [
                    "systemd-run", "--user", "--scope", "--quiet", "--collect",
                    f"--unit={unit}", "sleep", "300",
                ],
                cwd=str(ws),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            spawned.append((proc, unit))
            _wait_holder_cwd(proc, ws, unit=unit)
            return proc, unit

        proc = subprocess.Popen(
            ["sleep", "300"],
            cwd=str(ws),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append((proc, None))
        _wait_holder_cwd(proc, ws)
        real_read_cgroup = getattr(kbw, "_read_cgroup", None)

        def _fake_cgroup(pid: int) -> str:
            if pid == proc.pid:
                return "/user.slice/kbtest-foreign.scope"
            return real_read_cgroup(pid) if real_read_cgroup else ""

        monkeypatch.setattr(kbw, "_read_cgroup", _fake_cgroup, raising=False)
        return proc, None

    def spawn_child(ws: Path, *, env: Optional[dict[str, str]] = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            ["sleep", "300"],
            cwd=str(ws),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append((proc, None))
        _wait_holder_cwd(proc, ws)
        return proc

    try:
        yield spawn_foreign, spawn_child
    finally:
        for proc, unit in reversed(spawned):
            try:
                _stop_holder(proc, unit)
            except Exception:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)


def _make_scratch_task(conn, title: str = "scratch task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    task = kb.get_task(conn, tid)
    ws = kbw.resolve_workspace(task)
    kbw.set_workspace_path(conn, tid, ws)
    (ws / "keep.txt").write_text("important live data", encoding="utf-8")
    return tid, ws


def _events_of_kind(kind: str, task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id ASC",
            (task_id, kind),
        ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else {} for r in rows]


def _gc_args(event_days: int = 30, log_days: int = 30) -> argparse.Namespace:
    return argparse.Namespace(
        event_retention_days=event_days,
        log_retention_days=log_days,
    )


@pytest.mark.platforms("linux")
def test_complete_task_defers_for_real_foreign_holder_and_retries_after_exit(board, holder_spawner):
    spawn_foreign, _ = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "detached job task")
        proc, unit = spawn_foreign(ws, use_systemd=True)

        assert kb.complete_task(conn, tid, summary="launched detached job") is True

    assert ws.is_dir(), "workspace must survive while a live foreign process still uses it"
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "important live data"

    deferred = _events_of_kind("workspace_cleanup_deferred_live", tid)
    assert len(deferred) == 1
    assert deferred[0]["path"] == str(ws)
    matching = [h for h in deferred[0]["holders"] if h["pid"] == proc.pid]
    assert matching, f"expected pid {proc.pid} in holders: {deferred[0]['holders']}"
    assert matching[0]["via"] == "cwd"
    if unit:
        assert unit in matching[0]["unit"]

    _stop_holder(proc, unit)

    with kbc.connect_closing() as conn:
        retried = kb.retry_deferred_workspace_cleanups(conn)
    assert retried == [tid]
    assert not ws.exists()

    retried_events = _events_of_kind("workspace_cleanup_retried", tid)
    assert retried_events == [{"result": "removed"}]

    # Second retry call finds no candidates and appends no further events.
    with kbc.connect_closing() as conn:
        assert kb.retry_deferred_workspace_cleanups(conn) == []
    assert _events_of_kind("workspace_cleanup_retried", tid) == [{"result": "removed"}]


@pytest.mark.platforms("linux")
def test_own_current_process_cwd_does_not_block_cleanup(board):
    orig_cwd = os.getcwd()
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "self cwd task")
        os.chdir(ws)
        try:
            assert kb.complete_task(conn, tid, summary="done") is True
        finally:
            os.chdir(orig_cwd)
    assert not ws.exists()
    assert _events_of_kind("workspace_cleanup_deferred_live", tid) == []


@pytest.mark.platforms("linux")
def test_own_worker_scope_does_not_block_cleanup(board, holder_spawner, monkeypatch):
    _, spawn_child = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "worker scope task")
        proc = spawn_child(ws)
        self_pid = os.getpid()

        def _fake_cgroup(pid: int) -> str:
            if pid == self_pid:
                return "/test.slice/caller.scope"
            if pid == proc.pid:
                return f"/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-worker-kanban-{tid}-run-1.scope"
            return "/user.slice/unrelated.scope"

        monkeypatch.setattr(kbw, "_read_cgroup", _fake_cgroup)
        assert kb.complete_task(conn, tid, summary="done") is True
    assert not ws.exists()
    assert _events_of_kind("workspace_cleanup_deferred_live", tid) == []


@pytest.mark.platforms("linux")
def test_own_gateway_tagged_child_does_not_block_cleanup(board, holder_spawner, monkeypatch):
    _, spawn_child = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "gateway tagged task")
        child_env = {**os.environ, "HERMES_KANBAN_TASK": tid}
        proc = spawn_child(ws, env=child_env)
        self_pid = os.getpid()

        def _fake_cgroup(pid: int) -> str:
            if pid == self_pid:
                return "/test.slice/caller.scope"
            if pid == proc.pid:
                return "/system.slice/hermes-gateway.service"
            return "/user.slice/unrelated.scope"

        monkeypatch.setattr(kbw, "_read_cgroup", _fake_cgroup)
        assert kb.complete_task(conn, tid, summary="done") is True
    assert not ws.exists()
    assert _events_of_kind("workspace_cleanup_deferred_live", tid) == []


@pytest.mark.platforms("linux")
def test_tagged_background_scope_is_foreign_but_caller_cgroup_is_own(board, holder_spawner, monkeypatch):
    _, spawn_child = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn)
        child = spawn_child(ws, env={**os.environ, "HERMES_KANBAN_TASK": tid})
        background = "/user.slice/hermes-worker-proc_42.scope"
        monkeypatch.setattr(kbw, "_read_cgroup", lambda pid: background if pid == child.pid else "/caller.scope")
        assert kb.complete_task(conn, tid, summary="background job launched")
        assert ws.is_dir()
        assert [item["pid"] for item in _events_of_kind("workspace_cleanup_deferred_live", tid)[0]["holders"]] == [child.pid]
        monkeypatch.setattr(kbw, "_read_cgroup", lambda pid: background)
        assert kb.retry_deferred_workspace_cleanups(conn) == [tid]
        assert not ws.exists()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize(
    "child_task_env",
    [
        pytest.param("t_other999", id="other-task-tag"),
        pytest.param(None, id="untagged-gateway-proc"),
    ],
)
def test_gateway_child_without_matching_task_tag_is_foreign(
    board, holder_spawner, monkeypatch, child_task_env
):
    _, spawn_child = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "gateway foreign check")
        child_env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}
        if child_task_env is not None:
            child_env["HERMES_KANBAN_TASK"] = child_task_env
        proc = spawn_child(ws, env=child_env)
        self_pid = os.getpid()

        def _fake_cgroup(pid: int) -> str:
            if pid == self_pid:
                return "/test.slice/caller.scope"
            if pid == proc.pid:
                return "/system.slice/hermes-gateway.service"
            return "/user.slice/unrelated.scope"

        monkeypatch.setattr(kbw, "_read_cgroup", _fake_cgroup)
        assert kb.complete_task(conn, tid, summary="done") is True
    assert ws.is_dir()
    deferred = _events_of_kind("workspace_cleanup_deferred_live", tid)
    assert len(deferred) == 1
    assert any(h["pid"] == proc.pid for h in deferred[0]["holders"])


def test_no_proc_returns_empty_and_removes_workspace(board, tmp_path, monkeypatch):
    monkeypatch.setattr(kbw, "_PROC_ROOT", tmp_path / "no-proc-here")
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "no proc task")
        assert kbw._foreign_workspace_holders(tid, ws) == []
        assert kb.complete_task(conn, tid, summary="done") is True
    assert not ws.exists()


@pytest.mark.platforms("linux")
def test_archive_task_defers_for_live_holder_and_gc_cleans_after_exit(board, holder_spawner, capsys, monkeypatch):
    spawn_foreign, _ = holder_spawner
    with kbc.connect_closing() as conn:
        tid, ws = _make_scratch_task(conn, "archived with live holder")
        proc, unit = spawn_foreign(ws, use_systemd=True)
        assert kb.archive_task(conn, tid) is True
        conn.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (tid,))

    assert ws.is_dir()
    assert len(_events_of_kind("workspace_cleanup_deferred_live", tid)) == 1

    # Running gc while still held keeps the dir and avoids duplicate deferred events.
    assert kanban_ops._cmd_gc(_gc_args()) == 0
    assert ws.is_dir()
    assert len(_events_of_kind("workspace_cleanup_deferred_live", tid)) == 1
    assert _events_of_kind("workspace_cleanup_retried", tid) == []

    _stop_holder(proc, unit)
    capsys.readouterr()

    next_attempt = kbw.time.monotonic() + 31
    monkeypatch.setattr(kbw.time, "monotonic", lambda: next_attempt)

    assert kanban_ops._cmd_gc(_gc_args()) == 0
    out = capsys.readouterr().out
    assert "1 workspace(s)" in out
    assert not ws.exists()
    assert _events_of_kind("workspace_cleanup_retried", tid) == [{"result": "removed"}]


@pytest.mark.platforms("linux")
def test_cmd_gc_defers_archived_scratch_with_live_holder_and_cleans_after_exit(
    board, holder_spawner, capsys
):
    spawn_foreign, _ = holder_spawner
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="gc archived scratch")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived', workspace_kind='scratch' WHERE id=?",
                (tid,),
            )
        ws = kb.workspaces_root() / tid
        ws.mkdir(parents=True)
        (ws / "run.log").write_text("running", encoding="utf-8")
        proc, unit = spawn_foreign(ws, use_systemd=True)

    assert kanban_ops._cmd_gc(_gc_args()) == 0
    assert ws.is_dir()
    deferred = _events_of_kind("workspace_cleanup_deferred_live", tid)
    assert len(deferred) == 1
    assert any(h["pid"] == proc.pid for h in deferred[0]["holders"])

    _stop_holder(proc, unit)
    capsys.readouterr()

    assert kanban_ops._cmd_gc(_gc_args()) == 0
    out = capsys.readouterr().out
    assert "1 workspace(s)" in out
    assert not ws.exists()
    assert _events_of_kind("workspace_cleanup_retried", tid) == [{"result": "removed"}]


@pytest.mark.platforms("linux")
def test_parent_deferred_cleanup_defers_for_live_holder(board, holder_spawner):
    spawn_foreign, _ = holder_spawner
    with kbc.connect_closing() as conn:
        parent, parent_ws = _make_scratch_task(conn, "parent scratch")
        child = kb.create_task(conn, title="child task")
        kb.link_tasks(conn, parent, child)

        assert kb.complete_task(conn, parent, summary="parent done") is True
        assert parent_ws.is_dir(), "kept while child is active"

        proc, unit = spawn_foreign(parent_ws, use_systemd=True)
        assert kb.complete_task(conn, child, summary="child done") is True

    assert parent_ws.is_dir(), "parent workspace must be kept while foreign holder lives"
    deferred = _events_of_kind("workspace_cleanup_deferred_live", parent)
    assert len(deferred) == 1
    assert any(h["pid"] == proc.pid for h in deferred[0]["holders"])

    _stop_holder(proc, unit)
    with kbc.connect_closing() as conn:
        assert kb.retry_deferred_workspace_cleanups(conn) == [parent]
    assert not parent_ws.exists()


@pytest.mark.platforms("linux")
def test_worktree_cleanup_defers_for_live_holder_and_retries(board, tmp_path, holder_spawner):
    spawn_foreign, _ = holder_spawner
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e.st"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True, timeout=30)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "HEAD"], check=True, capture_output=True, timeout=30)

    with kbc.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="worktree with live holder",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        wt = kbw.resolve_workspace(kb.get_task(conn, tid))
        kbw.set_workspace_path(conn, tid, wt)
        proc, unit = spawn_foreign(wt, use_systemd=True)
        assert kb.complete_task(conn, tid, summary="done") is True

    assert wt.is_dir()
    deferred = _events_of_kind("workspace_cleanup_deferred_live", tid)
    assert len(deferred) == 1
    assert any(h["pid"] == proc.pid for h in deferred[0]["holders"])

    _stop_holder(proc, unit)
    with kbc.connect_closing() as conn:
        assert kb.retry_deferred_workspace_cleanups(conn) == [tid]
    assert not wt.exists()
    assert _events_of_kind("workspace_cleanup_retried", tid) == [{"result": "removed"}]


@pytest.mark.platforms("linux")
def test_synthetic_proc_detects_fd_map_deleted_and_ignores_root_slash(board, tmp_path, monkeypatch):
    fake_proc = tmp_path / "proc"
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    ws = kb.workspaces_root() / "t_synth1"
    ws.mkdir(parents=True)
    ws_real = os.path.realpath(ws)

    self_pid = os.getpid()
    (fake_proc / "self").mkdir(parents=True)
    (fake_proc / str(self_pid)).mkdir(parents=True)
    (fake_proc / str(self_pid) / "cgroup").write_text("0::/test.slice/self.scope\n", encoding="utf-8")

    # PID 41001: root is '/' (ignored), open fd points into ws with ' (deleted)' suffix.
    p1 = fake_proc / "41001"
    (p1 / "fd").mkdir(parents=True)
    os.symlink("/", p1 / "root")
    os.symlink(str(other_dir), p1 / "cwd")
    os.symlink(f"{ws_real}/out.log (deleted)", p1 / "fd" / "3")
    (p1 / "comm").write_text("writer\n", encoding="utf-8")
    (p1 / "cgroup").write_text("0::/user.slice/job-fd.service\n", encoding="utf-8")

    # PID 41002: memory-mapped file inside ws via /proc/<pid>/maps.
    p2 = fake_proc / "41002"
    p2.mkdir(parents=True)
    (p2 / "maps").write_text(
        "7f0000-7f1000 r-xp 00000000 08:01 123 /usr/lib/libc.so.6\n"
        f"7f2000-7f3000 r--p 00000000 08:01 456 {ws_real}/weights.bin (deleted)\n",
        encoding="utf-8",
    )
    (p2 / "comm").write_text("mapper\n", encoding="utf-8")
    (p2 / "cgroup").write_text("0::/user.slice/job-map.service\n", encoding="utf-8")

    # PID 41003: unrelated sibling path sharing prefix (t_synth10 must NOT match t_synth1).
    p3 = fake_proc / "41003"
    p3.mkdir(parents=True)
    os.symlink(f"{ws_real}0/file.txt", p3 / "cwd")
    (p3 / "comm").write_text("sibling\n", encoding="utf-8")
    (p3 / "cgroup").write_text("0::/user.slice/job-sibling.service\n", encoding="utf-8")

    monkeypatch.setattr(kbw, "_PROC_ROOT", fake_proc)
    holders = kbw._foreign_workspace_holders("t_synth1", ws)
    by_pid = {h["pid"]: h for h in holders}
    assert set(by_pid) == {41001, 41002}
    assert by_pid[41001] == {
        "pid": 41001,
        "comm": "writer",
        "unit": "job-fd.service",
        "cgroup": "/user.slice/job-fd.service",
        "via": "fd",
    }
    assert by_pid[41002] == {
        "pid": 41002,
        "comm": "mapper",
        "unit": "job-map.service",
        "cgroup": "/user.slice/job-map.service",
        "via": "map",
    }


def test_retry_handles_gone_shared_and_throttle(board, monkeypatch):
    with kbc.connect_closing() as conn:
        t_gone, ws_gone = _make_scratch_task(conn, "already gone")
        t_shared, ws_shared = _make_scratch_task(conn, "now shared")
        t_held, ws_held = _make_scratch_task(conn, "still held")
        live_sharer = kb.create_task(conn, title="live sharer")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id IN (?, ?, ?)", (t_gone, t_shared, t_held))
            conn.execute(
                "UPDATE tasks SET status='ready', workspace_kind='scratch', workspace_path=? WHERE id=?",
                (str(ws_shared), live_sharer),
            )
            for tid, ws in ((t_gone, ws_gone), (t_shared, ws_shared), (t_held, ws_held)):
                kb._append_event(
                    conn,
                    tid,
                    "workspace_cleanup_deferred_live",
                    {"path": str(ws), "holders": [{"pid": 99999, "comm": "sleep", "unit": "u.service", "cgroup": "/u.service", "via": "cwd"}]},
                )

        shutil.rmtree(ws_gone)

        scan_calls: list[str] = []

        def _fake_holders(task_id: str, path, *, limit: int = 10) -> list[dict]:
            scan_calls.append(task_id)
            return [{"pid": 99999, "comm": "sleep", "unit": "u.service", "cgroup": "/u.service", "via": "cwd"}]

        monkeypatch.setattr(kbw, "_foreign_workspace_holders", _fake_holders)
        clock = [1000.0]
        monkeypatch.setattr(kbw.time, "monotonic", lambda: clock[0])

        assert kb.retry_deferred_workspace_cleanups(conn) == []
        assert scan_calls == [t_held]
        assert _events_of_kind("workspace_cleanup_retried", t_gone) == [{"result": "gone"}]
        assert _events_of_kind("workspace_cleanup_retried", t_shared) == [{"result": "shared"}]
        assert ws_shared.is_dir()
        assert ws_held.is_dir()
        assert _events_of_kind("workspace_cleanup_retried", t_held) == []

        # Within 30s throttle window: t_held is skipped without re-scanning.
        clock[0] = 1015.0
        assert kb.retry_deferred_workspace_cleanups(conn) == []
        assert scan_calls == [t_held]

        # After 30s window and holder gone: t_held is scanned and removed.
        clock[0] = 1031.0
        monkeypatch.setattr(kbw, "_foreign_workspace_holders", lambda task_id, path, *, limit=10: [])
        assert kb.retry_deferred_workspace_cleanups(conn) == [t_held]
        assert not ws_held.exists()
        assert _events_of_kind("workspace_cleanup_retried", t_held) == [{"result": "removed"}]


@pytest.mark.platforms("linux")
def test_retry_batches_roots_and_pages_past_persistent_holders(board, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    job = proc / "41234"
    job.mkdir()
    (job / "cgroup").write_text("4:cpu:/v1/cpu\n3:memory:/v1/foreign.scope\n")
    (job / "comm").write_text("holder\n")
    monkeypatch.setattr(kbw, "_PROC_ROOT", proc)
    real_scandir = os.scandir
    scans = []

    def count_scans(path):
        if path == proc:
            scans.append(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", count_scans)
    with kbc.connect_closing() as conn:
        first, second, free = [_make_scratch_task(conn) for _ in range(3)]
        for tid, path in (first, second, free):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "workspace_cleanup_deferred_live", {"path": str(path), "holders": []})
        (job / "root").symlink_to(first[1])
        (job / "exe").symlink_to(second[1] / "program")

        assert kb.retry_deferred_workspace_cleanups(conn, limit=2) == []
        assert scans == [proc]
        assert first[1].is_dir() and second[1].is_dir() and free[1].is_dir()
        next_tick = kbw.time.monotonic() + 60
        monkeypatch.setattr(kbw.time, "monotonic", lambda: next_tick)
        assert kb.retry_deferred_workspace_cleanups(conn, limit=2) == [free[0]]
        assert not free[1].exists()
        assert scans == [proc, proc]
        for task, via in ((first, "root"), (second, "exe")):
            holder, = kbw._foreign_workspace_holders(*task, limit=1)
            assert (holder["via"], holder["unit"]) == (via, "foreign.scope")


@pytest.mark.platforms("linux")
def test_probe_matches_lexical_and_resolved_spellings(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    monkeypatch.setattr(kbw, "_PROC_ROOT", proc)
    for pid, target in ((42001, real), (42002, alias), (42003, tmp_path / "alias-more")):
        directory = proc / str(pid)
        directory.mkdir()
        (directory / "cgroup").write_text("2:cpu:/v1\n0::/v2/foreign.scope\n3:memory:/last\n")
        (directory / "comm").write_text("job\n")
        (directory / "cwd").symlink_to(str(target) + " (deleted)")
    holders = kbw._foreign_workspace_holders("t_alias", alias)
    assert {(item["pid"], item["cgroup"]) for item in holders} == {(42001, "/v2/foreign.scope"), (42002, "/v2/foreign.scope")}


def test_retry_does_not_remove_a_workspace_that_was_reopened_during_scan(board, monkeypatch):
    with kbc.connect_closing() as conn:
        task_id, workspace = _make_scratch_task(conn)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "workspace_cleanup_deferred_live", {"path": str(workspace), "holders": []})

        def reopen(task_id, path):
            with kbc.connect_closing() as other:
                other.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            return []

        monkeypatch.setattr(kbw, "_foreign_workspace_holders", reopen)
        assert kb.retry_deferred_workspace_cleanups(conn) == []
        assert workspace.is_dir()
        assert _events_of_kind("workspace_cleanup_retried", task_id) == [{"result": "task_changed"}]


def test_dispatcher_reclaim_calls_retry_and_survives_exception(board, monkeypatch):
    with kbc.connect_closing() as conn:
        calls: list[object] = []

        def _ok_retry(c, *, limit=20):
            calls.append(c)
            return ["t_retried1"]

        monkeypatch.setattr(kbw, "retry_deferred_workspace_cleanups", _ok_retry)
        res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)
        assert len(calls) == 1
        assert res.retried_workspace_cleanups == ["t_retried1"]

        def _boom_retry(c, *, limit=20):
            raise RuntimeError("transient /proc failure")

        monkeypatch.setattr(kbw, "retry_deferred_workspace_cleanups", _boom_retry)
        res2 = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 0, dry_run=True, max_spawn=0)
        assert res2.retried_workspace_cleanups == []
