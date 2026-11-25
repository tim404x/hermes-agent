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


def _build_fleet_with_canonical_links(tmp_path: Path) -> tuple[Path, Path]:
    fleet = tmp_path / ".hermes"
    fleet.mkdir(mode=0o700, exist_ok=True)
    (fleet / "skills" / "demo" / "refs").mkdir(parents=True, mode=0o700, exist_ok=True)
    (fleet / "skills" / "demo" / "SKILL.md").write_text("demo skill\n", encoding="utf-8")
    (fleet / "skills" / "demo" / "refs" / "target.md").write_text("guide\n", encoding="utf-8")
    (fleet / "skills" / "demo" / "refs" / "alias.md").symlink_to("target.md")
    (fleet / "skills" / "demo" / "refs" / "up.md").symlink_to("../SKILL.md")

    (fleet / "plugins" / "sample").mkdir(parents=True, mode=0o700)
    (fleet / "plugins" / "sample" / "data.txt").write_text("plugin-data\n", encoding="utf-8")

    (fleet / "memories").mkdir(mode=0o700, exist_ok=True)
    (fleet / "memories" / "MEMORY.md").write_text("fleet-memory\n", encoding="utf-8")
    (fleet / "SOUL.md").write_text("fleet-soul\n", encoding="utf-8")

    profile = fleet / "profiles" / "alpha"
    profile.mkdir(parents=True, mode=0o700)
    (profile / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")
    (profile / ".env").write_text("ALPHA_KEY=fixture\n", encoding="utf-8")
    for asset in ("skills", "plugins", "memories", "SOUL.md"):
        (profile / asset).symlink_to(f"../../{asset}")
    return fleet, profile


@pytest.mark.platforms("linux")
def test_canonical_relative_links_project_ro_assets_and_copy_mutable_assets(tmp_path):
    from hermes_cli.kanban_isolation.home import (
        plan_attempt_home,
        prepare_attempt_home_plan,
        validate_public_asset_sources,
    )

    fleet, profile = _build_fleet_with_canonical_links(tmp_path)
    pub = validate_public_asset_sources(profile)
    assert pub.fleet_root == fleet.resolve()
    assert {p.name for p in pub.readonly_projections} == {"skills", "plugins"}
    assert all(p.aliased for p in pub.readonly_projections)
    assert {c.name for c in pub.copy_assets} == {"memories", "SOUL.md"}
    assert all(c.aliased for c in pub.copy_assets)

    attempt = fleet / "kanban-worker-plane" / "t_links" / "run-1" / "home"
    plan = prepare_attempt_home_plan(attempt, profile)
    assert plan == plan_attempt_home(attempt, profile)

    # Shared skills/plugins are NOT copied; empty 0o700 mountpoints await BindReadOnlyPaths.
    ro_by_name = {p.name: p for p in plan.readonly_projections}
    assert set(ro_by_name) == {"skills", "plugins"}
    for name in ("skills", "plugins"):
        proj = ro_by_name[name]
        assert proj.source == (fleet / name).resolve()
        assert proj.target == (attempt / name).resolve()
        assert proj.bind_spec == f"{proj.source}:{proj.target}"
        mountpoint = attempt / name
        assert mountpoint.is_dir() and not mountpoint.is_symlink()
        assert (mountpoint.lstat().st_mode & 0o077) == 0
        assert list(mountpoint.iterdir()) == []
    assert ro_by_name["skills"].stats.links == 2

    # Shared memories and SOUL.md MUST be privately copied with zero write-through.
    assert not (attempt / "memories").is_symlink()
    assert not (attempt / "SOUL.md").is_symlink()
    assert (attempt / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "fleet-memory\n"
    assert (attempt / "SOUL.md").read_text(encoding="utf-8") == "fleet-soul\n"
    (attempt / "memories" / "MEMORY.md").write_text("attempt-private-memory\n", encoding="utf-8")
    (attempt / "SOUL.md").write_text("attempt-private-soul\n", encoding="utf-8")
    assert (fleet / "memories" / "MEMORY.md").read_text(encoding="utf-8") == "fleet-memory\n"
    assert (fleet / "SOUL.md").read_text(encoding="utf-8") == "fleet-soul\n"


@pytest.mark.platforms("linux")
@pytest.mark.parametrize(
    "case",
    [
        "default_profile_alias",
        "nonstandard_parent_alias",
        "cross_profile_alias",
        "wrong_asset_alias",
        "absolute_top_level_alias",
        "chained_top_level_alias",
        "private_config_alias",
        "private_env_alias",
        "private_auth_alias",
        "nested_absolute_inside_skills",
        "nested_escaping_inside_skills",
        "nested_escape_and_reenter",
        "nested_chained_escape",
        "nested_loop",
        "nested_dangling",
        "nested_fifo_direct",
        "nested_fifo_target",
        "nested_link_in_memories",
    ],
)
def test_disallowed_source_aliases_and_nested_targets_fail_closed(tmp_path, case):
    import os

    from hermes_cli.kanban_isolation.home import plan_attempt_home

    fleet, profile = _build_fleet_with_canonical_links(tmp_path)
    other = fleet / "profiles" / "beta"
    other.mkdir(parents=True, mode=0o700)
    (other / "skills").mkdir(mode=0o700)
    (other / "memories").mkdir(mode=0o700)

    target_profile = profile
    if case == "default_profile_alias":
        outside = tmp_path / "outside-skills"
        outside.mkdir(mode=0o700)
        (fleet / "skills").rename(tmp_path / "orig-skills")
        (fleet / "skills").symlink_to("../outside-skills")
        target_profile = fleet
    elif case == "nonstandard_parent_alias":
        custom = tmp_path / "custom" / "alpha"
        custom.mkdir(parents=True, mode=0o700)
        (custom / "skills").symlink_to("../../.hermes/skills")
        target_profile = custom
    elif case == "cross_profile_alias":
        (profile / "skills").unlink()
        (profile / "skills").symlink_to("../beta/skills")
    elif case == "wrong_asset_alias":
        (profile / "skills").unlink()
        (profile / "skills").symlink_to("../../plugins")
    elif case == "absolute_top_level_alias":
        (profile / "skills").unlink()
        (profile / "skills").symlink_to((fleet / "skills").resolve())
    elif case == "chained_top_level_alias":
        real_skills = tmp_path / "real-skills"
        (fleet / "skills").rename(real_skills)
        (fleet / "skills").symlink_to(real_skills)
    elif case == "private_config_alias":
        (fleet / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        (profile / "config.yaml").unlink()
        (profile / "config.yaml").symlink_to("../../config.yaml")
    elif case == "private_env_alias":
        (fleet / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (profile / ".env").unlink()
        (profile / ".env").symlink_to("../../.env")
    elif case == "private_auth_alias":
        (fleet / "auth.json").write_text("{}\n", encoding="utf-8")
        (profile / "auth.json").symlink_to("../../auth.json")
    elif case == "nested_absolute_inside_skills":
        (fleet / "skills" / "demo" / "abs.md").symlink_to((fleet / "skills" / "demo" / "SKILL.md").resolve())
    elif case == "nested_escaping_inside_skills":
        (fleet / "skills" / "demo" / "esc.md").symlink_to("../../../SOUL.md")
    elif case == "nested_escape_and_reenter":
        (fleet / "skills" / "demo" / "bounce.md").symlink_to("../../skills/demo/SKILL.md")
    elif case == "nested_chained_escape":
        (fleet / "skills" / "demo" / "hop1.md").symlink_to("hop2.md")
        (fleet / "skills" / "demo" / "hop2.md").symlink_to("../../../SOUL.md")
    elif case == "nested_loop":
        (fleet / "skills" / "demo" / "a.md").symlink_to("b.md")
        (fleet / "skills" / "demo" / "b.md").symlink_to("a.md")
    elif case == "nested_dangling":
        (fleet / "skills" / "demo" / "broken.md").symlink_to("does-not-exist.md")
    elif case == "nested_fifo_direct":
        os.mkfifo(fleet / "skills" / "demo" / "pipe")
    elif case == "nested_fifo_target":
        os.mkfifo(fleet / "skills" / "demo" / "pipe")
        (fleet / "skills" / "demo" / "to-pipe").symlink_to("pipe")
    elif case == "nested_link_in_memories":
        (fleet / "memories" / "link.md").symlink_to("MEMORY.md")

    attempt = fleet / "kanban-worker-plane" / "t_bad" / "run-1" / "home"
    with pytest.raises(ValueError):
        plan_attempt_home(attempt, target_profile)


@pytest.mark.parametrize(
    "bad_segment",
    ["has space", "has:colon", "has%spec", 'has"quote', "has'quote", "has\\slash", "has\ttab"],
)
def test_unsafe_systemd_bind_paths_are_rejected_unambiguously(bad_segment):
    from hermes_cli.kanban_isolation.home import format_bind_readonly_path, validate_systemd_bind_path

    bad_path = Path(f"/tmp/{bad_segment}/skills")
    with pytest.raises(ValueError, match="unsafe systemd BindReadOnlyPaths path"):
        validate_systemd_bind_path(bad_path)
    with pytest.raises(ValueError, match="unsafe systemd BindReadOnlyPaths path"):
        format_bind_readonly_path(bad_path, Path("/tmp/safe/skills"))
    with pytest.raises(ValueError, match="unsafe systemd BindReadOnlyPaths path"):
        format_bind_readonly_path(Path("/tmp/safe/skills"), bad_path)


@pytest.mark.platforms("linux")
def test_verify_required_readonly_mounts_fails_closed_when_not_ro(tmp_path):
    import os

    from hermes_cli.kanban_isolation.mounts import verify_required_readonly_mounts

    attempt = tmp_path / "home"
    skills = attempt / "skills"
    skills.mkdir(parents=True, mode=0o700)
    st = skills.lstat()
    manifest = [{
        "name": "skills",
        "source": str(tmp_path / "src-skills"),
        "target": str(skills),
        "source_dev": st.st_dev,
        "source_ino": st.st_ino,
    }]
    ro_mountinfo = f"99 28 8:1 /src {skills} ro,nosuid,nodev,relatime - ext4 /dev/sda1 rw\n"
    rw_mountinfo = f"99 28 8:1 /src {skills} rw,nosuid,nodev,relatime - ext4 /dev/sda1 rw\n"
    ro_vfs = SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1))
    rw_vfs = SimpleNamespace(f_flag=0)

    assert verify_required_readonly_mounts(
        attempt, manifest, mountinfo_text=ro_mountinfo, statvfs_fn=lambda _p: ro_vfs, access_fn=lambda _p, _m: False
    ) == ["skills"]

    with pytest.raises(RuntimeError, match="mountinfo"):
        verify_required_readonly_mounts(
            attempt, manifest, mountinfo_text=rw_mountinfo, statvfs_fn=lambda _p: ro_vfs, access_fn=lambda _p, _m: False
        )
    with pytest.raises(RuntimeError, match="ST_RDONLY"):
        verify_required_readonly_mounts(
            attempt, manifest, mountinfo_text=ro_mountinfo, statvfs_fn=lambda _p: rw_vfs, access_fn=lambda _p, _m: False
        )
    with pytest.raises(RuntimeError, match="writable"):
        verify_required_readonly_mounts(
            attempt, manifest, mountinfo_text=ro_mountinfo, statvfs_fn=lambda _p: ro_vfs, access_fn=lambda _p, _m: True
        )
    with pytest.raises(RuntimeError, match="inode"):
        bad_manifest = [dict(manifest[0], source_ino=st.st_ino + 1)]
        verify_required_readonly_mounts(
            attempt, bad_manifest, mountinfo_text=ro_mountinfo, statvfs_fn=lambda _p: ro_vfs, access_fn=lambda _p, _m: False
        )
    with pytest.raises(RuntimeError, match="unverified projected asset"):
        verify_required_readonly_mounts(
            attempt, [], mountinfo_text=ro_mountinfo, statvfs_fn=lambda _p: ro_vfs, access_fn=lambda _p, _m: False
        )


@pytest.mark.platforms("linux")
def test_launch_passes_bind_readonly_paths_and_fails_closed_without_copy_fallback(isolated_board, monkeypatch):
    home, conn = isolated_board
    fleet, profile = _build_fleet_with_canonical_links(home.parent)
    assert fleet == home
    tid = kb.create_task(conn, title="bind launch", assignee="alpha")
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None

    monkeypatch.setattr(kwp, "systemd_user_service_available", lambda env=None: (True, ""))
    monkeypatch.setattr(kwp, "_command", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("gateway.drain_control.current_instantiation_epoch", lambda: "boot-1")
    monkeypatch.setattr(kwp, "isolated_unit_state", lambda _intent: kwp.UnitState.GONE)

    seen_cmd: list[str] = []
    call_order: list[str] = []
    orig_prepare = kwp.prepare_attempt_home_plan
    orig_intent = kwp._record_launch_intent

    def trace_prepare(attempt_home, profile_home):
        call_order.append("prepare")
        return orig_prepare(attempt_home, profile_home)

    def trace_intent(database, task, metadata):
        call_order.append("record_intent")
        return orig_intent(database, task, metadata)

    monkeypatch.setattr(kwp, "prepare_attempt_home_plan", trace_prepare)
    monkeypatch.setattr(kwp, "_record_launch_intent", trace_intent)

    def fake_run(argv, **kwargs):
        call_order.append("systemd_run")
        seen_cmd.extend(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="Failed to set up mount namespacing: Operation not permitted")

    monkeypatch.setattr(kwp.subprocess, "run", fake_run)

    from tools.process_registry import RestartSafeScopeUnavailable

    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home.parent),
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_DB": str(home / "kanban.db"),
        "HERMES_KANBAN_WORKSPACES_ROOT": str(home / "workspaces"),
        "HERMES_KANBAN_TASK": claimed.id,
        "HERMES_KANBAN_RUN_ID": str(claimed.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": claimed.claim_lock,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    with pytest.raises(RestartSafeScopeUnavailable, match="mount namespacing"):
        kwp.launch_isolated_worker_service(
            task=claimed,
            workspace=str(home),
            board="default",
            board_db=home / "kanban.db",
            profile_home=profile,
            env=base_env,
            log_path=home / "worker.log",
        )

    attempt = kwp.attempt_home_dir(home / "kanban.db", claimed.id, int(claimed.current_run_id))
    assert call_order == ["prepare", "record_intent", "systemd_run"]
    assert f"BindReadOnlyPaths={(fleet / 'skills').resolve()}:{attempt / 'skills'}" in seen_cmd
    assert f"BindReadOnlyPaths={(fleet / 'plugins').resolve()}:{attempt / 'plugins'}" in seen_cmd
    # Never falls back to copying skills/plugins on mount failure.
    assert list((attempt / "skills").iterdir()) == []
    assert list((attempt / "plugins").iterdir()) == []


@pytest.mark.platforms("linux")
def test_systemd_probe_verifies_readonly_bind_mount_live(tmp_path):
    kwp.reset_systemd_service_probe_cache()
    available, reason = kwp.systemd_user_service_available()
    if not available:
        pytest.skip(f"systemd user mount namespace unavailable in test runner: {reason}")
    assert available is True
    assert reason == ""
