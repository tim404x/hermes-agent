"""How far a fork-tracking checkout trails NousResearch, for the update check and the update button.

An install that tracks a fork (``origin`` = the user's fork, carries rebased onto NousResearch by an
integrator such as ``~/.hermes/bin/hermes-integrate.py``) only ever fast-forwards to the fork. When
the integrator fails, the fork freezes and every check prints "Already up to date" while upstream
moves on for weeks. This note makes that visible: how many NousResearch commits the checkout lacks
and how the last integration run ended.

Local-only: counts against the ``nous/main`` (or ``upstream/main``) remote-tracking ref the
integrator keeps fresh, never touches the network, and returns None for a non-fork install, a
checkout with no upstream ref, or one that already contains upstream's tip.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

_UPSTREAM_REFS = ("refs/remotes/nous/main", "refs/remotes/upstream/main")
_OFFICIAL = "nousresearch/hermes-agent"


def _git(root: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout.strip() if p.returncode == 0 else None


def _integration_state_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "state" / "hermes-integrate.json"


def _last_integration(state_path: Path | None = None) -> str:
    """``ok at <time>`` / ``failed: <reason> at <time>`` from the integrator's state, or ``unknown``."""
    try:
        state = json.loads((state_path or _integration_state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unknown (no integrator state)"
    if not isinstance(state, dict):
        return "unknown (no integrator state)"
    when = state.get("finished") or state.get("started") or "unknown time"
    outcome = state.get("outcome")
    if outcome == "success":
        return f"ok at {when}"
    if outcome == "failed":
        reason = " ".join(str(state.get("error") or "unknown error").split())
        reason = reason.split(" tail:")[0][:160]
        attempts = state.get("attempts_on_tip")
        extra = f", {attempts} run(s) on this upstream tip" if attempts else ""
        return f"failed: {reason} at {when}{extra}"
    return f"{outcome or 'unknown'} at {when}"


def fork_integration_note(root: Path | str, *, state_path: Path | None = None) -> str | None:
    """``"NousResearch is N commits ahead; last integration: <ok|failed: reason> at <time>"`` or None."""
    root = Path(root)
    origin = (_git(root, "remote", "get-url", "origin") or "").lower()
    if not origin or _OFFICIAL in origin:
        return None  # origin IS NousResearch: "up to date" already means upstream's tip
    ref = next((r for r in _UPSTREAM_REFS if _git(root, "rev-parse", "--verify", "--quiet", r)), None)
    if ref is None:
        return None
    count = _git(root, "rev-list", "--count", f"HEAD..{ref}")
    if count is None or not count.isdigit() or int(count) == 0:
        return None
    ahead = int(count)
    return (f"NousResearch is {ahead} commit{'s' if ahead != 1 else ''} ahead; "
            f"last integration: {_last_integration(state_path)}")


def print_fork_integration_note(root: Path | str) -> None:
    """The CLI's ``hermes update --check`` line; never raises."""
    try:
        note = fork_integration_note(root)
    except Exception:  # an informational line must never break the check
        return
    if note:
        print(f"  ℹ {note}")
