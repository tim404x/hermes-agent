"""Focused regression tests for the opt-in isolated systemd Kanban worker plane."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_plane as kwp
from hermes_cli.kanban_isolation.home import prepare_attempt_home


@pytest.fixture
def isolated_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kwp.reset_systemd_service_probe_cache()
    kb.init_db()
    conn = kbc.connect()
    try:
        yield home, conn
    finally:
        conn.close()
        kwp.reset_systemd_service_probe_cache()


def test_worker_plane_config_defaults_and_fail_closed(isolated_board):
    home, _conn = isolated_board
    assert kwp.resolve_worker_plane({}) == kwp.WORKER_PLANE_PROFILE_OWNER
    assert kwp.resolve_worker_plane({"worker_plane": ""}) == kwp.WORKER_PLANE_PROFILE_OWNER
    assert kwp.resolve_worker_plane({"worker_plane": "profile_owner"}) == kwp.WORKER_PLANE_PROFILE_OWNER
    assert kwp.resolve_worker_plane({"worker_plane": "isolated_systemd"}) == kwp.WORKER_PLANE_ISOLATED_SYSTEMD
    with pytest.raises(ValueError, match="unsupported kanban.worker_plane"):
        kwp.resolve_worker_plane({"worker_plane": "bogus_plane"})

    # Through config loader with temp config.yaml
    (home / "config.yaml").write_text("kanban:\n  worker_plane: isolated_systemd\n", encoding="utf-8")
    assert kwp.resolve_worker_plane() == kwp.WORKER_PLANE_ISOLATED_SYSTEMD


@pytest.mark.platforms('linux')
def test_prepare_attempt_home_isolates_mutable_state_from_profile_home(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    (profile / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")
    (profile / "SOUL.md").write_text("soul\n", encoding="utf-8")
    (profile / "skills").mkdir()
    (profile / "state.db").write_text("shared-db", encoding="utf-8")
    (profile / "gateway.lock").write_text("shared-lock", encoding="utf-8")
    (profile / "sessions").mkdir()

    attempt = tmp_path / "board" / "kanban-worker-plane" / "t_1" / "run-1" / "home"
    prepare_attempt_home(attempt, profile)

    assert (attempt.stat().st_mode & 0o077) == 0
    for name in ('.env', 'auth.json', 'memories/MEMORY.md'):
        source = profile / name
        source.parent.mkdir(exist_ok=True)
        source.write_text('original')
    second = attempt.with_name('other-home')
    prepare_attempt_home(second, profile)
    for name in ('config.yaml', 'SOUL.md', '.env', 'auth.json', 'memories/MEMORY.md'):
        original = (profile / name).read_bytes()
        (second / name).write_text('attempt-only')
        prepare_attempt_home(second, profile)
        assert (second / name).read_text() == 'attempt-only'
        assert (profile / name).read_bytes() == original
    (profile / 'skills' / 'escape').symlink_to(profile / '.env')
    with pytest.raises(ValueError, match='linked'):
        prepare_attempt_home(attempt.with_name('unsafe'), profile)
    (second / 'skills' / 'planted').symlink_to(profile / '.env')
    with pytest.raises(PermissionError):
        prepare_attempt_home(second, profile)
    # Mutable authority files MUST NOT be symlinked to the shared profile home.
    assert not (attempt / "state.db").exists() and not (attempt / "state.db").is_symlink()
    assert not (attempt / "gateway.lock").exists() and not (attempt / "gateway.lock").is_symlink()
    assert not (attempt / "sessions").exists() and not (attempt / "sessions").is_symlink()


def test_isolated_systemd_fails_closed_without_burning_failure_budget(isolated_board, monkeypatch):
    home, conn = isolated_board
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:9/v1\n  default: test-model\n"
        "kanban:\n  worker_plane: isolated_systemd\n",
        encoding="utf-8",
    )
    tid = kb.create_task(conn, title="needs systemd", assignee="default")
    kb.recompute_ready(conn)
    monkeypatch.setattr(kwp, "systemd_user_service_available", lambda env=None: (False, "no user bus"))

    res = kbd.dispatch_once(conn, max_spawn=1, failure_limit=1)

    assert not res.spawned
    assert not res.auto_blocked
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "ready"
    assert task.consecutive_failures == 0
    events = kb.list_events(conn, tid)
    assert any(e.kind == "spawn_failed" and (e.payload or {}).get("infrastructure") is True for e in events)


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('fault', ['unreadable', 'expected_inv', 'actual_inv', 'foreign_inv', 'boot', 'actual_boot', 'foreign_boot', 'birth', 'cgroup', 'unit', 'marker_only', 'conflict'])
def test_unverified_unit_cannot_signal_or_release_claim(isolated_board, monkeypatch, fault):
    home, conn = isolated_board
    task_id = kb.create_task(conn, title='live claim', assignee='default', max_runtime_seconds=1)
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, task_id)
    run_id = claimed.current_run_id
    unit = kwp.isolated_worker_unit_name(home / 'kanban.db', task_id, run_id)
    group = '/private/' + unit
    metadata = {'worker_plane': 'isolated_systemd', 'systemd_unit': unit, 'invocation_id': 'ours', 'boot_id': 'boot'}
    props = {'Id': unit, 'ActiveState': 'active', 'LoadState': 'loaded', 'InvocationID': 'ours',
             'MainPID': '99999999', 'ControlGroup': group}
    if fault == 'expected_inv':
        metadata['invocation_id'] = ''
    if fault == 'boot':
        metadata['boot_id'] = ''
    if fault == 'unit':
        metadata['systemd_unit'] = 'unexpected.service'
    props['InvocationID'] = {'actual_inv': '', 'foreign_inv': 'different'}.get(fault, 'ours')
    monkeypatch.setattr('gateway.drain_control.current_instantiation_epoch', lambda: {'actual_boot': '', 'foreign_boot': 'new'}.get(fault, 'boot'))
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: 'boot|123')
    if fault == 'marker_only':
        metadata = {}
        kb._append_event(conn, task_id, 'unit_requested', None, run_id=run_id)
    if fault == 'conflict':
        kb._append_event(conn, task_id, 'worker_registered', dict(metadata, invocation_id='different'), run_id=run_id)
    kbd._set_worker_pid(conn, task_id, 99999999, run_id=run_id, spawn_metadata=metadata)
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: 'boot|234' if fault == 'birth' else 'boot|123')
    monkeypatch.setattr(kwp, 'read_current_cgroup_path', lambda pid: '/other' if fault == 'cgroup' else group)

    def inspect(name):
        assert not conn.in_transaction
        return {} if fault == 'unreadable' else props

    monkeypatch.setattr(kwp, 'inspect_systemd_unit', inspect)
    identity = kwp.run_worker_plane_identity(conn, task_id)
    result = kwp.stop_isolated_worker_unit(identity)
    assert result['unit_state'] == 'unknown' and not result['unit_stop_attempted'] and not result['terminated']
    def never_signal(*args):
        pytest.fail('an isolated run must never be signalled by PID')
    old = int(time.time()) - 36000
    conn.execute('UPDATE tasks SET started_at=?,claim_expires=?,last_heartbeat_at=? WHERE id=?', (old, old, old, task_id))
    conn.execute('UPDATE task_runs SET started_at=? WHERE id=?', (old, run_id))
    with monkeypatch.context() as live_pid:
        live_pid.setattr(kb, '_pid_alive', lambda pid: pid == 99999999)
        assert not kb.reclaim_task(conn, task_id, signal_fn=never_signal)
        assert not kbd.enforce_max_runtime(conn, signal_fn=never_signal)
    assert not kb.reclaim_task(conn, task_id, signal_fn=never_signal)
    assert not kbd.enforce_max_runtime(conn, signal_fn=never_signal)
    assert not kbd.detect_stale_running(conn, stale_timeout_seconds=1, signal_fn=never_signal)
    assert not kb.release_stale_claims(conn, signal_fn=never_signal)
    assert not kbd._reclaim_dead_workers(conn).crashed
    assert kb.get_task(conn, task_id).current_run_id == run_id
    assert kb.claim_task(conn, task_id) is None
    assert kb.archive_task(conn, task_id, signal_fn=never_signal)
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
    assert kb.claim_task(conn, task_id) is None
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
    assert kb.claim_review_task(conn, task_id) is None
    props.update(LoadState='not-found', ActiveState='inactive', MainPID='0', ControlGroup='')
    if fault not in ('unit', 'unreadable', 'marker_only'):
        assert kb.claim_review_task(conn, task_id).current_run_id != run_id


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('after,terminated', [('unreadable', False), ('inactive', True), ('not-found', True)])
def test_stop_requires_observed_unit_exit(monkeypatch, after, terminated):
    name = 'hermes-kanban-w-fixture.service'
    group = '/private/' + name
    identity = {'worker_plane': 'isolated_systemd', 'systemd_unit': name, 'invocation_id': 'ours',
                'boot_id': 'boot', 'pid': 99999999, 'started_at': 'birth'}
    active = {'Id': name, 'LoadState': 'loaded', 'ActiveState': 'active', 'InvocationID': 'ours',
              'MainPID': '99999999', 'ControlGroup': group}
    gone = {'Id': name, 'LoadState': 'loaded' if after == 'inactive' else 'not-found',
            'ActiveState': 'inactive', 'MainPID': '0', 'ControlGroup': ''}
    states = iter([active, {} if after == 'unreadable' else gone])
    monkeypatch.setattr(kwp, 'inspect_systemd_unit', lambda name: next(states))
    monkeypatch.setattr('gateway.drain_control.current_instantiation_epoch', lambda: 'boot')
    monkeypatch.setattr(kbd, '_process_fingerprint', lambda pid: 'birth')
    monkeypatch.setattr(kwp, 'read_current_cgroup_path', lambda pid: group)
    monkeypatch.setattr(kwp, '_command', lambda _: '/bin/systemctl')
    sent = []
    monkeypatch.setattr(kwp.subprocess, 'run', lambda cmd, **kwargs: sent.append(cmd))
    result = kwp.stop_isolated_worker_unit(identity)
    assert sent == [['/bin/systemctl', '--user', 'stop', name]]
    assert result['unit_stop_attempted'] and result['terminated'] is terminated


@pytest.mark.platforms('linux')
def test_source_profile_home_preserves_policy_allowlist_and_credential_isolation(isolated_board, monkeypatch):
    home, conn = isolated_board
    # Create a named profile "alpha" with its own secret and command_allowlist.
    alpha = home / "profiles" / "alpha"
    alpha.mkdir(parents=True, mode=0o700)
    (alpha / "config.yaml").write_text(
        "model:\n  provider: custom\n  base_url: http://127.0.0.1:18080/v1\n  default: alpha-model\n"
        "command_allowlist:\n  - 'git status'\n",
        encoding="utf-8",
    )
    (alpha / ".env").write_text("OPENAI_API_KEY=sk-alpha-only\nALPHA_ONLY_TOKEN=tok-alpha\n", encoding="utf-8")
    (home / ".env").write_text("OPENAI_API_KEY=sk-default-leak\nDEFAULT_ONLY_SECRET=leak-me-not\n", encoding="utf-8")
    monkeypatch.setenv("DEFAULT_ONLY_SECRET", "leak-me-not")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-default-leak")
    monkeypatch.setenv('MANAGER_CUSTOM', 'fixture-manager')
    monkeypatch.setenv('SSH_AUTH_SOCK', str(home / 'synthetic-agent'))

    attempt_home = home / "kanban-worker-plane" / "t_pol" / "run-1" / "home"
    prepare_attempt_home(attempt_home, alpha)

    from gateway.session_managed_worker import _worker_config, _worker_env
    from gateway.session_policy import LocalSessionPolicy

    authority = SimpleNamespace(profile_id=str(attempt_home), source_profile_home=str(alpha))
    env = _worker_env(authority)
    assert env is not None
    assert env["HERMES_HOME"] == str(attempt_home)
    assert env.get("OPENAI_API_KEY") == "sk-alpha-only"
    assert env.get("ALPHA_ONLY_TOKEN") == "tok-alpha"
    assert "DEFAULT_ONLY_SECRET" not in env
    assert 'MANAGER_CUSTOM' not in env
    assert 'SSH_AUTH_SOCK' not in env

    policy = LocalSessionPolicy(
        source="kanban",
        platform="cli",
        cwd=str(home),
        model="alpha-model",
        toolsets=("terminal", "kanban"),
        config_json=json.dumps({"model": {"provider": "custom", "default": "alpha-model"}}),
        request_json=json.dumps({"source": "kanban"}),
        terminal_json=json.dumps({}),
    )
    resolved_cfg = _worker_config(authority, policy)
    assert resolved_cfg.get("command_allowlist") == ["git status"]
    (alpha / 'config.yaml').write_text('command_allowlist: []\n')
    assert _worker_config(authority, policy).get('command_allowlist') == []

    from gateway.session_kanban import build_kanban_policy
    from persistence.core.errors import AuthorityRefusal
    actor = SimpleNamespace(profile_id=str(attempt_home), capabilities={'session:create'})
    connection = SimpleNamespace(native_owner=True, authority=authority, actor=actor)
    task_id = kb.create_task(conn, title='belongs to default', assignee='default')
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, task_id)
    params = dict(board='default', db=str(home / 'kanban.db'), task_id=task_id,
                  run_id=claimed.current_run_id, claim_lock=claimed.claim_lock)
    with pytest.raises(AuthorityRefusal, match='invalid_kanban_claim') as refused:
        build_kanban_policy(connection, params, {})
    assert refused.value.__cause__.reason == 'profile_mismatch'


@pytest.mark.platforms('linux')
def test_clean_exec_discards_the_manager_environment(tmp_path):
    from hermes_cli.kanban_isolation import clean_exec
    manifest = tmp_path / 'launch.json'
    manifest.write_text(json.dumps({'HOME': str(tmp_path), 'ASSIGNEE_ONLY': 'fixture'}))
    check = ('import os,sys; sys.exit(not (os.environ["ASSIGNEE_ONLY"] == "fixture" '
             'and os.environ["INVOCATION_ID"] == "our-unit" and "MANAGER_ONLY" not in os.environ))')
    subprocess.run([sys.executable, '-IS', clean_exec.__file__, str(manifest), sys.executable, '-IS', '-c', check],
                   env={'INVOCATION_ID': 'our-unit', 'MANAGER_ONLY': 'synthetic'}, check=True, timeout=10)


def test_attempt_session_summary_exposes_session_and_unit_visibility(isolated_board):
    _home, conn = isolated_board
    tid = kb.create_task(conn, title="vis test", assignee="default")
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    run_id = claimed.current_run_id
    kb._append_event(
        conn, tid, "owner_admitted",
        {
            "db": "/tmp/attempt/state.db",
            "session_id": "local-abc123",
            "request_id": f'kanban:["default","{tid}",{run_id}]',
            "pid": 1234,
            "birth": 555.0,
            "worker_plane": kwp.WORKER_PLANE_ISOLATED_SYSTEMD,
            "systemd_unit": "hermes-kanban-w-00112233-t_vis-r1.service",
            "invocation_id": "inv-1",
            "boot_id": "boot-1",
        },
        run_id=run_id,
    )
    kb._append_event(
        conn, tid, "worker_bound",
        {
            "pid": 1235,
            "claim_lock": claimed.claim_lock,
            "started_at": "boot-1|999",
            "cgroup": "/user.slice/hermes-kanban-w-00112233-t_vis-r1.service",
        },
        run_id=run_id,
    )
    kb._append_event(
        conn, tid, "worker_result",
        {"pid": 1235, "claim_lock": claimed.claim_lock, "exit_code": 0, "last_output": "done"},
        run_id=run_id,
    )
    conn.commit()

    summary = kwp.kanban_attempt_session_summary(conn, tid, run_id)
    assert summary is not None
    assert summary["session_id"] == "local-abc123"
    assert summary["owner_db"] == "/tmp/attempt/state.db"
    assert summary["systemd_unit"] == "hermes-kanban-w-00112233-t_vis-r1.service"
    assert summary["bound_pid"] == 1235
    assert summary["bound_cgroup"] == "/user.slice/hermes-kanban-w-00112233-t_vis-r1.service"
    assert summary["exit_code"] == 0
