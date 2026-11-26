"""POSIX permission invariants for tool-result archives and rich_sent_index.json."""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway import rich_sent_store
from hermes_constants import hermes_home_key
import tools.tool_result_storage as trs
from tools.tool_result_storage import (
    PRUNED_SUBDIR,
    SPILLOVER_SUBDIR,
    _sweep_archive_dir,
    _write_to_spillover,
    write_pruned_original,
)

pytestmark = pytest.mark.platforms("posix")


def _mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture(autouse=True)
def _isolated_posix_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", raising=False)
    monkeypatch.setattr(trs, "_spillover_pruned_homes", set())
    monkeypatch.setattr(trs, "_tightened_archive_dirs", set(), raising=False)
    old_umask = os.umask(0o022)
    try:
        yield tmp_path
    finally:
        os.umask(old_umask)


def test_write_pruned_original_is_private_and_byte_exact(tmp_path):
    body = '{"token": "sk-test-secret-1234567890abcdef", "lines": "a\\nb"}'
    archived = write_pruned_original(body, "s1", "read_file", "call_1")
    assert archived is not None
    host_path, _stub_path = archived

    pruned_dir = tmp_path / PRUNED_SUBDIR
    assert _mode(pruned_dir) == 0o700
    assert _mode(host_path) == 0o600
    with open(host_path, "rb") as fh:
        assert fh.read() == body.encode("utf-8")


def test_spillover_non_exclusive_overwrite_tightens_preexisting_file(tmp_path):
    spill_dir = tmp_path / SPILLOVER_SUBDIR
    spill_dir.mkdir(parents=True)
    os.chmod(spill_dir, 0o755)
    target = spill_dir / "tc_overwrite.txt"
    target.write_bytes(b"old loose content with a longer tail")
    os.chmod(target, 0o644)
    assert _mode(target) == 0o644

    written = _write_to_spillover("new private bytes", target.name)
    assert written == str(target)
    assert _mode(spill_dir) == 0o700
    assert _mode(target) == 0o600
    assert target.read_bytes() == b"new private bytes"


def test_spillover_failed_fchmod_preserves_existing_bytes(tmp_path):
    target = tmp_path / SPILLOVER_SUBDIR / "tc_overwrite.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original bytes")

    with patch("tools.tool_result_storage.os.fchmod", side_effect=OSError("chmod failed")):
        assert _write_to_spillover("replacement", target.name) is None
    assert target.read_bytes() == b"original bytes"


def test_preexisting_loose_dir_and_kept_file_migrated_by_write_and_sweep(tmp_path, monkeypatch):
    pruned_dir = tmp_path / PRUNED_SUBDIR
    pruned_dir.mkdir(parents=True)
    os.chmod(pruned_dir, 0o755)
    old_file = pruned_dir / "legacy_archive.txt"
    old_file.write_bytes(b"legacy archive body")
    os.chmod(old_file, 0o644)

    outside = tmp_path / "outside_untouched.txt"
    outside.write_bytes(b"outside")
    os.chmod(outside, 0o644)
    os.symlink(outside, pruned_dir / "planted_link.txt")

    # Skip the post-write automatic prune so we verify dir tightening and sweep tightening separately.
    monkeypatch.setattr(trs, "_spillover_pruned_homes", {hermes_home_key()})

    archived = write_pruned_original("fresh archive", "s1", "terminal", "call_2")
    assert archived is not None
    assert _mode(pruned_dir) == 0o700
    assert _mode(old_file) == 0o644

    # Second write does not re-chmod the already-tightened existing dir.
    real_chmod = os.chmod
    chmod_calls = []

    def tracking_chmod(path, mode, *a, **kw):
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode, *a, **kw)

    with patch("tools.tool_result_storage.os.chmod", side_effect=tracking_chmod):
        assert write_pruned_original("second archive", "s1", "terminal", "call_3") is not None
    assert chmod_calls == []

    # Retention sweep with a cutoff that keeps old_file tightens its mode to 0600 without deleting it.
    removed = _sweep_archive_dir(pruned_dir, cutoff=time.time() - 3600)
    assert removed == 0
    assert old_file.exists()
    assert old_file.read_bytes() == b"legacy archive body"
    assert _mode(old_file) == 0o600
    # Symlink target outside archive dir must never be chmod'd.
    assert _mode(outside) == 0o644


def test_expired_loose_archive_tightened_when_unlink_fails(tmp_path):
    target = tmp_path / SPILLOVER_SUBDIR / "expired.txt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"expired archive")
    target.chmod(0o644)
    cutoff = target.stat().st_mtime + 1
    unlink = Path.unlink

    def deny_target_unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError(errno.EACCES, "Cannot unlink", str(path))
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", deny_target_unlink):
        assert _sweep_archive_dir(target.parent, cutoff) == 0
    assert target.read_bytes() == b"expired archive"
    assert _mode(target) == 0o600


@pytest.mark.parametrize(
    ("subdir", "exclusive"),
    [(SPILLOVER_SUBDIR, False), (PRUNED_SUBDIR, True)],
)
def test_planted_symlink_at_target_fails_closed(tmp_path, subdir, exclusive):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"untouched outside data")
    os.chmod(outside, 0o644)

    archive_dir = tmp_path / subdir
    archive_dir.mkdir(parents=True)
    link = archive_dir / "target.txt"
    os.symlink(outside, link)

    assert _write_to_spillover("overwrite attempt", "target.txt", subdir=subdir, exclusive=exclusive) is None
    assert outside.read_bytes() == b"untouched outside data"
    assert _mode(outside) == 0o644
    assert link.is_symlink()


def test_docker_without_host_user_keeps_umask_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "false")

    spill_path = _write_to_spillover("docker spill", "tc_docker.txt")
    assert spill_path is not None
    assert _mode(tmp_path / SPILLOVER_SUBDIR) == 0o755
    assert _mode(spill_path) == 0o644

    archived = write_pruned_original("docker pruned", "s1", "read_file", "call_d1")
    assert archived is not None
    host_path, _ = archived
    assert _mode(tmp_path / PRUNED_SUBDIR) == 0o755
    assert _mode(host_path) == 0o644

    # Retention sweep also respects the docker exception (no chmod).
    assert _sweep_archive_dir(tmp_path / PRUNED_SUBDIR, cutoff=time.time() - 3600) == 0
    assert _mode(host_path) == 0o644


def test_docker_with_host_user_writes_private_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "true")

    spill_path = _write_to_spillover("docker host-user spill", "tc_docker_priv.txt")
    assert spill_path is not None
    assert _mode(tmp_path / SPILLOVER_SUBDIR) == 0o700
    assert _mode(spill_path) == 0o600

    archived = write_pruned_original("docker host-user pruned", "s1", "read_file", "call_d2")
    assert archived is not None
    host_path, _ = archived
    assert _mode(tmp_path / PRUNED_SUBDIR) == 0o700
    assert _mode(host_path) == 0o600


def test_docker_without_host_user_skips_private_archive_dir(tmp_path, monkeypatch, caplog):
    archived = write_pruned_original("local body", "s1", "terminal", "local_call")
    assert archived is not None
    archive_dir = tmp_path / PRUNED_SUBDIR
    assert _mode(archive_dir) == 0o700
    (tmp_path / SPILLOVER_SUBDIR).symlink_to(archive_dir, target_is_directory=True)

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_DOCKER_RUN_AS_HOST_USER", "false")
    with caplog.at_level(logging.WARNING, logger=trs.__name__):
        assert write_pruned_original("docker prune", "s1", "terminal", "docker_call") is None
        assert _write_to_spillover("docker spill", "docker.txt") is None

    assert _mode(archive_dir) == 0o700
    assert list(archive_dir.iterdir()) == [Path(archived[0])]
    warnings = [rec.getMessage() for rec in caplog.records
                if rec.name == trs.__name__ and rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(archive_dir) in warnings[0]
    assert "terminal.docker_run_as_host_user: true" in warnings[0]


@pytest.mark.parametrize("backend", ["ssh", "modal", "daytona", "vercel_sandbox"])
def test_remote_upload_backends_write_private_archives(tmp_path, monkeypatch, backend):
    monkeypatch.setenv("TERMINAL_ENV", backend)
    archived = write_pruned_original("remote body", "s1", "read_file", "call_r1")
    assert archived is not None
    host_path, _ = archived
    assert _mode(tmp_path / PRUNED_SUBDIR) == 0o700
    assert _mode(host_path) == 0o600


def test_dir_chmod_eperm_does_not_fail_write(tmp_path):
    pruned_dir = tmp_path / PRUNED_SUBDIR
    pruned_dir.mkdir(parents=True)
    os.chmod(pruned_dir, 0o755)

    def fail_chmod(path, mode, *a, **kw):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

    with patch("tools.tool_result_storage.os.chmod", side_effect=fail_chmod):
        archived = write_pruned_original("still written", "s1", "read_file", "call_e1")
    assert archived is not None
    host_path, _ = archived
    assert _mode(host_path) == 0o600


def test_rich_sent_store_writes_private_index_under_umask_022(tmp_path):
    assert rich_sent_store.record("chat_1", "msg_1", "private message body") is True
    index_path = tmp_path / "state" / "rich_sent_index.json"
    assert index_path.exists()
    assert _mode(tmp_path / "state") == 0o700
    assert _mode(index_path) == 0o600
    assert rich_sent_store.lookup("chat_1", "msg_1") == "private message body"


def test_rich_sent_store_tightens_existing_file_without_chmodding_existing_state_dir(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    os.chmod(state_dir, 0o755)
    index_path = state_dir / "rich_sent_index.json"
    index_path.write_text("{}", encoding="utf-8")
    os.chmod(index_path, 0o644)

    assert rich_sent_store.record("chat_2", "msg_2", "updated private body") is True
    assert _mode(state_dir) == 0o755
    assert _mode(index_path) == 0o600
    assert rich_sent_store.lookup("chat_2", "msg_2") == "updated private body"
