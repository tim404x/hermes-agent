"""Kanban descendants keep an attribution-only copy of the worker's task id.

``$HERMES_KANBAN_TASK`` is scrubbed from every descendant (worker identity stays with the
dispatcher-spawned worker), which left task shells and scripts with no way to stamp the card
that launched them. ``HERMES_KANBAN_ORIGIN_TASK`` / ``_RUN_ID`` carry it without granting scope.
"""
import json
import os
import shlex
import subprocess
import sys

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER, KANBAN_ORIGIN_RUN_ID_ENV, KANBAN_ORIGIN_TASK_ENV,
    delegated_child_subprocess_env, scrub_kanban_env,
)
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tools.environments.local import LocalEnvironment


def _worker(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    conn = connect(db)
    tid = kb.create_task(conn, title="own")
    kb.claim_task(conn, tid)
    task = kb.get_task(conn, tid)
    for key, value in {
        "HERMES_KANBAN_DB": str(db), "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock, "HOME": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    for key in (DELEGATED_CHILD_ENV_MARKER, KANBAN_ORIGIN_TASK_ENV, KANBAN_ORIGIN_RUN_ID_ENV):
        monkeypatch.delenv(key, raising=False)
    return conn, tid, str(task.current_run_id)


def test_worker_terminal_sees_its_card_as_origin_not_identity(tmp_path, monkeypatch):
    """The 26 Nov 2026 report: ``env | grep KANBAN_TASK`` was empty in a worker's terminal."""
    conn, tid, run_id = _worker(tmp_path, monkeypatch)
    terminal = LocalEnvironment(cwd=str(tmp_path))
    try:
        out = terminal.execute(
            'echo "origin=${HERMES_KANBAN_ORIGIN_TASK-unset} run=${HERMES_KANBAN_ORIGIN_RUN_ID-unset} '
            'task=${HERMES_KANBAN_TASK-unset} fence=${HERMES_DELEGATED_CHILD_CONTEXT:+set}"')["output"]
    finally:
        terminal.cleanup()
    assert f"origin={tid} run={run_id} task=unset fence=set" in out, out
    conn.close()


def test_origin_survives_grandchildren_and_grants_no_scope(tmp_path, monkeypatch):
    conn, tid, run_id = _worker(tmp_path, monkeypatch)
    child = delegated_child_subprocess_env(dict(os.environ))
    assert child[KANBAN_ORIGIN_TASK_ENV] == tid and child[KANBAN_ORIGIN_RUN_ID_ENV] == run_id
    assert "HERMES_KANBAN_TASK" not in child and child[DELEGATED_CHILD_ENV_MARKER]
    probe = (
        "import json, os, sys\n"
        "from agent.delegation_context import delegated_child_subprocess_env, is_dispatcher_owned_worker_context\n"
        "from tools import kanban_tools as kt\n"
        "g = delegated_child_subprocess_env(dict(os.environ))\n"
        "print('ROW=' + json.dumps({'owner': is_dispatcher_owned_worker_context(), 'default': kt._default_task_id(None),"
        " 'grand': g.get('HERMES_KANBAN_ORIGIN_TASK'), 'grand_run': g.get('HERMES_KANBAN_ORIGIN_RUN_ID'),"
        " 'grand_task': g.get('HERMES_KANBAN_TASK')}))\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child["PYTHONPATH"] = root
    proc = subprocess.run([sys.executable, "-c", probe], env=child, cwd=root, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    row = json.loads(next(l.split("ROW=", 1)[1] for l in proc.stdout.splitlines() if l.startswith("ROW=")))
    assert row == {"owner": False, "default": None, "grand": tid, "grand_run": run_id, "grand_task": None}, row
    conn.close()


def test_execute_code_child_carries_origin(tmp_path, monkeypatch):
    from tools.code_execution_env import _build_child_env

    conn, tid, run_id = _worker(tmp_path, monkeypatch)
    env = _build_child_env(rpc_endpoint="fixture", rpc_token="fixture", tmpdir=str(tmp_path),
                           child_python=sys.executable)
    assert env[KANBAN_ORIGIN_TASK_ENV] == tid and env[KANBAN_ORIGIN_RUN_ID_ENV] == run_id
    assert "HERMES_KANBAN_TASK" not in env
    conn.close()


def test_a_real_task_replaces_a_stale_inherited_origin_whole(monkeypatch):
    """Task and run id always come from one source: a worker spawned under an old origin never
    pairs its own task with the old run id."""
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)
    stale = {KANBAN_ORIGIN_TASK_ENV: "t_old", KANBAN_ORIGIN_RUN_ID_ENV: "7"}
    assert scrub_kanban_env({**stale, "HERMES_KANBAN_TASK": "t_new"})[KANBAN_ORIGIN_TASK_ENV] == "t_new"
    assert KANBAN_ORIGIN_RUN_ID_ENV not in scrub_kanban_env({**stale, "HERMES_KANBAN_TASK": "t_new"})
    kept = scrub_kanban_env(dict(stale))
    assert (kept[KANBAN_ORIGIN_TASK_ENV], kept[KANBAN_ORIGIN_RUN_ID_ENV]) == ("t_old", "7")
