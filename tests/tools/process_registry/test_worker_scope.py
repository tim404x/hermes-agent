"""Managed workers of a supervised gateway put background jobs in their own hermes-worker scope.

A managed worker (``python -m agent.managed_worker``) runs each agent turn inside the gateway's
service cgroup but does not own the gateway PID file. Before this, its background jobs ran
uncapped in ``hermes-gateway.service`` (a 46 GB proof squeezed the host until memguard killed it).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from tools.process_registry import ProcessRegistry
from tools.process_registry_ownership import in_supervised_gateway_tree


@pytest.fixture()
def registry(monkeypatch):
    """Fake Popen pids never reach the owner-death supervisor: a live pgid would start it."""
    monkeypatch.setattr("hermes_cli.process_identity._update_supervised_pgids", lambda verb, pgids: None)
    return ProcessRegistry()


def _spawn_managed_worker_job(registry, monkeypatch, tmp_path, use_pty):
    """Spawn as a registered managed worker that is NOT the gateway PID-file owner."""
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("INVOCATION_ID", "gateway-service-invocation")
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *, cleanup_stale=False: os.getpid() + 1)
    monkeypatch.setattr("agent.runtime_session_store.is_worker_process", lambda: True)
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    monkeypatch.setattr("tools.process_registry._find_shell", lambda: "/bin/bash")
    monkeypatch.setattr("tools.process_registry._systemd_run_user_scope_available", lambda: True)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/systemd-run")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        proc = MagicMock(pid=4321, stdout=iter([]), stdin=MagicMock())
        proc.poll.return_value = None
        return proc

    with (
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("threading.Thread", return_value=MagicMock()),
        patch.object(registry, "_write_checkpoint"),
    ):
        session = registry.spawn_local("echo hello", cwd=str(tmp_path), use_pty=use_pty)
    # A worker always takes the pipe + output-log path (its pipe would die with the worker).
    assert session.output_log == str(tmp_path / f"{session.id}.log")
    return session, captured["argv"]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("use_pty", [False, True])
def test_managed_worker_background_job_is_scoped(registry, monkeypatch, tmp_path, use_pty):
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    session, argv = _spawn_managed_worker_job(registry, monkeypatch, tmp_path, use_pty)

    assert argv[0] == "/usr/bin/systemd-run"
    assert "--scope" in argv
    assert argv[argv.index("--unit") + 1] == f"hermes-worker-{session.id}"
    assert session.systemd_unit == f"hermes-worker-{session.id}.scope"


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("use_pty", [False, True])
def test_managed_worker_of_unsupervised_gateway_is_not_scoped(registry, monkeypatch, tmp_path, use_pty):
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda environ=None: False)
    session, argv = _spawn_managed_worker_job(registry, monkeypatch, tmp_path, use_pty)

    assert argv[:2] == ["/bin/bash", "-lic"]
    assert session.systemd_unit == ""


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("owns_pid,worker,gateway_marker,expected", [
    pytest.param(True, False, True, True, id="gateway-pid-owner"),
    pytest.param(False, True, True, True, id="managed-worker"),
    pytest.param(False, True, False, False, id="worker-outside-gateway"),
    pytest.param(False, False, True, False, id="cli-descendant"),
])
def test_in_supervised_gateway_tree(monkeypatch, owns_pid, worker, gateway_marker, expected):
    gateway_pid = os.getpid() if owns_pid else os.getpid() + 1
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *, cleanup_stale=False: gateway_pid)
    monkeypatch.setattr("agent.runtime_session_store.is_worker_process", lambda: worker)
    monkeypatch.setattr("gateway.restart.is_gateway_supervisor_process", lambda environ=None: False)
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    if gateway_marker:
        monkeypatch.setenv("_HERMES_GATEWAY", "1")
    else:
        monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    assert in_supervised_gateway_tree() is expected
