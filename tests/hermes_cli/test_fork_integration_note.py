"""A fork-tracking install's update check says how far NousResearch is and how integration went.

Before: the button and ``hermes update --check`` compared only against the fork and printed
"up to date" for two weeks while every integration run failed and upstream moved 8,758 commits on.
"""
import json
import subprocess

import pytest

from hermes_cli.fork_integration import fork_integration_note


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", "https://github.com/someone/hermes-agent.git")

    def commit(msg):
        (root / "f.txt").write_text(msg)
        _git(root, "add", "f.txt")
        _git(root, "commit", "-q", "-m", msg)
        return _git(root, "rev-parse", "HEAD")

    base = commit("base")
    up1, up2 = commit("upstream 1"), commit("upstream 2")
    _git(root, "update-ref", "refs/remotes/nous/main", up2)
    _git(root, "reset", "-q", "--hard", base)
    commit("carry")
    return root, up1, up2


def _state(tmp_path, **state):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_reports_upstream_lead_and_failed_integration(repo, tmp_path):
    root, _, _ = repo
    state = _state(tmp_path, outcome="failed", finished="2026-10-29T12:29:24Z", attempts_on_tip=2,
                   error="gates failed:\n  carry marker missing: per-job bot_chat_title tail: noise")
    note = fork_integration_note(root, state_path=state)
    assert note == ("NousResearch is 2 commits ahead; last integration: failed: gates failed: carry marker "
                    "missing: per-job bot_chat_title at 2026-10-29T12:29:24Z, 2 run(s) on this upstream tip")


def test_reports_ok_integration(repo, tmp_path):
    root, _, _ = repo
    note = fork_integration_note(root, state_path=_state(tmp_path, outcome="success", finished="T1"))
    assert note == "NousResearch is 2 commits ahead; last integration: ok at T1"


def test_missing_state_is_unknown_not_silent(repo, tmp_path):
    root, _, _ = repo
    note = fork_integration_note(root, state_path=tmp_path / "nope.json")
    assert note is not None and note.endswith("last integration: unknown (no integrator state)")


def test_silent_when_upstream_tip_is_contained(repo, tmp_path):
    root, _, _ = repo
    _git(root, "update-ref", "refs/remotes/nous/main", _git(root, "rev-parse", "HEAD~1"))
    assert fork_integration_note(root, state_path=_state(tmp_path, outcome="failed")) is None


def test_silent_without_an_upstream_ref_or_on_the_official_repo(repo, tmp_path):
    root, _, _ = repo
    state = _state(tmp_path, outcome="failed")
    _git(root, "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")
    assert fork_integration_note(root, state_path=state) is None
    _git(root, "remote", "set-url", "origin", "https://github.com/someone/hermes-agent.git")
    _git(root, "update-ref", "-d", "refs/remotes/nous/main")
    assert fork_integration_note(root, state_path=state) is None


@pytest.mark.asyncio
async def test_update_check_endpoint_carries_the_note(monkeypatch):
    """The desktop button's check: "latest version" becomes "latest version of your fork" + note."""
    from hermes_cli import fork_integration, source_check
    from hermes_cli.web_routers import actions

    monkeypatch.setattr(actions, "is_commit_build", lambda *_a: False)
    monkeypatch.setattr(actions, "_dashboard_local_update_managed_externally", lambda: False)
    monkeypatch.setattr(actions, "detect_install_method", lambda *_a: "git")
    monkeypatch.setattr(source_check, "check_for_updates", lambda **_k: {"behind": 0})
    monkeypatch.setattr(fork_integration, "fork_integration_note",
                        lambda *_a, **_k: "NousResearch is 5 commits ahead; last integration: ok at T1")
    payload = await actions.check_hermes_update(force=True)
    assert payload["behind"] == 0 and payload["update_available"] is False
    assert payload["upstream_note"] == "NousResearch is 5 commits ahead; last integration: ok at T1"
    assert payload["message"] == ("You're on the latest version of your fork. "
                                  "NousResearch is 5 commits ahead; last integration: ok at T1.")

    monkeypatch.setattr(fork_integration, "fork_integration_note", lambda *_a, **_k: None)
    payload = await actions.check_hermes_update(force=True)
    assert payload["message"] == "You're on the latest version." and "upstream_note" not in payload
