"""Skill readiness for ``required_credential_files``: existence decides readiness, the sandbox
mount guard only decides mounting.

Regression (24 Nov 2026): google-workspace's token files existed and worked, but they were
symlinks into a protected ``google-creds/`` directory, so the mount guard refused them and
``register_credential_files`` reported them as *missing*. ``skill_view`` then said
``setup_needed`` and the cron preflight blocked every job attaching the skill
(``blocked_config: ... missing credential file google_token.json``).
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

import tools.credential_files as _cred_mod
from tools.credential_files import clear_credential_files, get_credential_file_mounts


@pytest.fixture(autouse=True)
def _clean_state():
    clear_credential_files()
    _cred_mod._config_files = {}
    yield
    clear_credential_files()
    _cred_mod._config_files = {}


_SKILL = "gw-test-skill"


def _make_home(tmp_path):
    """A HERMES_HOME laid out like the live VPS: root token names are symlinks into google-creds/."""
    home = tmp_path / ".hermes"
    creds = home / "google-creds"
    creds.mkdir(parents=True)
    (creds / "google_token_someone@example.com.json").write_text('{"token": "x"}')
    (creds / "google_token.json").symlink_to("google_token_someone@example.com.json")
    (creds / "google_client_secret.json").write_text('{"installed": {}}')
    (home / "google_token.json").symlink_to(creds / "google_token.json")
    (home / "google_client_secret.json").symlink_to(creds / "google_client_secret.json")
    (home / ".env").write_text("SECRET=1\n")
    skills = home / "skills"
    skill_dir = skills / _SKILL
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {_SKILL}\n"
        "description: Test skill with credential files\n"
        "required_credential_files:\n"
        "  - path: google_token.json\n"
        "    description: OAuth token\n"
        "  - path: google_client_secret.json\n"
        "    description: OAuth client\n"
        "---\n\n# body\n"
    )
    return home, skills


def _view(home, skills, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills)
    from tools.skills_tool import skill_view

    return json.loads(skill_view(_SKILL))


def _preflight(home, skills, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills)
    from cron.scheduler_preflight import _preflight_check_skills

    return _preflight_check_skills({"skills": [_SKILL]})


@pytest.mark.platforms("posix")
class TestExistingButUnmountableIsReady:
    def test_guard_really_refuses_these_files(self, tmp_path, monkeypatch):
        """Precondition: the layout reproduces the live refusal (else the tests below prove nothing)."""
        home, _skills = _make_home(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(home))
        assert _cred_mod.register_credential_file("google_token.json") is False
        assert _cred_mod.register_credential_file("google_client_secret.json") is False

    def test_skill_view_reports_available(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        payload = _view(home, skills, monkeypatch)
        assert payload["success"] is True
        assert payload["missing_credential_files"] == []
        assert payload["setup_needed"] is False
        assert payload["readiness_status"] == "available"

    def test_cron_preflight_does_not_block(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        assert _preflight(home, skills, monkeypatch) is None

    def test_guard_stays_intact_nothing_is_mounted(self, tmp_path, monkeypatch):
        """Readiness must not widen the mount surface: refused files stay unmounted."""
        home, skills = _make_home(tmp_path)
        _view(home, skills, monkeypatch)
        assert get_credential_file_mounts() == []

    def test_master_store_declared_by_a_skill_is_never_mounted(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        (skills / _SKILL / "SKILL.md").write_text(
            "---\n"
            f"name: {_SKILL}\n"
            "description: Hostile skill\n"
            "required_credential_files:\n"
            "  - .env\n"
            "---\n\n# body\n"
        )
        _view(home, skills, monkeypatch)
        assert get_credential_file_mounts() == []

    def test_refusal_is_surfaced_on_remote_backends(self, tmp_path, monkeypatch):
        """On a sandbox backend the skill is told which present files will not reach the sandbox."""
        home, skills = _make_home(tmp_path)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        payload = _view(home, skills, monkeypatch)
        assert payload["setup_needed"] is False
        assert sorted(payload.get("unmountable_credential_files") or []) == [
            "google_client_secret.json", "google_token.json"]
        assert "google_token.json" in payload.get("credential_files_note", "")


@pytest.mark.platforms("posix")
class TestGenuinelyMissingStillFlags:
    def test_absent_file_is_missing(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        (home / "google_token.json").unlink()
        payload = _view(home, skills, monkeypatch)
        assert payload["missing_credential_files"] == ["google_token.json"]
        assert payload["setup_needed"] is True
        assert payload["readiness_status"] == "setup_needed"

    def test_dangling_symlink_is_missing(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        (home / "google-creds" / "google_token_someone@example.com.json").unlink()
        payload = _view(home, skills, monkeypatch)
        assert payload["missing_credential_files"] == ["google_token.json"]
        assert payload["setup_needed"] is True

    def test_cron_preflight_blocks_on_absent_file(self, tmp_path, monkeypatch):
        home, skills = _make_home(tmp_path)
        (home / "google_client_secret.json").unlink()
        verdict = _preflight(home, skills, monkeypatch)
        assert verdict is not None
        assert "credential file google_client_secret.json" in verdict
        assert "google_token.json" not in verdict

    def test_path_outside_home_counts_as_missing(self, tmp_path, monkeypatch):
        """An absolute / escaping path can never be satisfied, so it stays a setup problem."""
        home, skills = _make_home(tmp_path)
        (skills / _SKILL / "SKILL.md").write_text(
            "---\n"
            f"name: {_SKILL}\n"
            "description: Bad path\n"
            "required_credential_files:\n"
            "  - ../outside.json\n"
            "---\n\n# body\n"
        )
        (tmp_path / "outside.json").write_text("{}")
        payload = _view(home, skills, monkeypatch)
        assert payload["missing_credential_files"] == ["../outside.json"]
        assert payload["setup_needed"] is True


def test_register_report_separates_missing_from_refused(tmp_path):
    home, _skills = _make_home(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        report = _cred_mod.register_credential_files_report(
            ["google_token.json", "nope.json", ".env"])
    assert report.missing == ["nope.json"]
    assert sorted(report.refused) == [".env", "google_token.json"]
    assert get_credential_file_mounts() == []
