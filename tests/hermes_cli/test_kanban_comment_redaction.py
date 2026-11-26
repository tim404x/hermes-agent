"""Domain-boundary redaction for kanban comments at write time.

``hermes_cli.kanban_db.add_comment`` is the shared write path for the CLI,
agent tool, dashboard API and swarm blackboard; redacting there ensures no
secret written into a comment lands in ``kanban.db`` or replays via
``kanban_show`` / ``list_comments``.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_swarm import (
    SwarmWorkerSpec,
    create_swarm,
    latest_blackboard,
    post_blackboard_update,
)


def _synthetic_secret() -> str:
    """Runtime-generated high-entropy bearer/secret with guaranteed mixed classes."""
    return "aZ" + secrets.token_urlsafe(24) + "9Q"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a clean kanban DB."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants

        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _latest_commented_len(conn, task_id: str) -> int:
    events = [ev for ev in kb.list_events(conn, task_id) if ev.kind == "commented"]
    assert events
    payload = events[-1].payload
    assert isinstance(payload, dict)
    return int(payload["len"])


def test_add_comment_redacts_bearer_query_link_and_records_stored_len(kanban_home):
    value = _synthetic_secret()
    body = f"Inspect https://eye.example.net/?t={value}&r=timr for the capture."
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="bearer link comment")
        kb.add_comment(conn, tid, author="worker", body=body)

        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1
        stored = comments[0].body
        assert stored == "Inspect https://eye.example.net/?t=***&r=timr for the capture."
        assert value not in stored
        assert value[:6] not in stored
        assert value[-4:] not in stored
        assert _latest_commented_len(conn, tid) == len(stored)


def test_add_comment_redacts_env_password_and_json_secret_without_hints(kanban_home):
    env_secret = _synthetic_secret()
    json_secret = _synthetic_secret()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="assignment and json secret comments")

        kb.add_comment(conn, tid, author="worker", body=f"DB_PASSWORD={env_secret}")
        env_len = _latest_commented_len(conn, tid)

        kb.add_comment(conn, tid, author="worker", body=json.dumps({"secret": json_secret}))
        json_len = _latest_commented_len(conn, tid)

        comments = kb.list_comments(conn, tid)
        assert len(comments) == 2

        env_stored, json_stored = comments[0].body, comments[1].body
        assert env_stored == "DB_PASSWORD=***"
        for fragment in (env_secret, env_secret[:6], env_secret[-4:]):
            assert fragment not in env_stored
        assert env_len == len(env_stored)

        assert json.loads(json_stored) == {"secret": "***"}
        for fragment in (json_secret, json_secret[:6], json_secret[-4:]):
            assert fragment not in json_stored
        assert json_len == len(json_stored)


def test_add_comment_preserves_benign_negatives_byte_for_byte(kanban_home):
    git_sha = secrets.token_hex(20)
    notion_page_id = secrets.token_hex(16)
    negatives = [
        "see https://youtu.be/x?t=30s",
        (
            "Updated the rollout checklist in "
            f"[Q4 Delivery Plan](https://www.notion.so/workspace/Q4-Delivery-Plan-{notion_page_id}) "
            "before review."
        ),
        (
            f"Verified commit {git_sha} in hermes_cli/kanban_db.py and "
            "tests/hermes_cli/test_kanban_db.py."
        ),
    ]
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="benign comments")
        for expected in negatives:
            kb.add_comment(conn, tid, author="worker", body=expected)
            assert _latest_commented_len(conn, tid) == len(expected)

        stored = [c.body for c in kb.list_comments(conn, tid)]
        assert stored == negatives


def test_add_comment_keeps_empty_and_whitespace_validation(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="validation")
        for blank in ("", "   \n\t  "):
            with pytest.raises(ValueError, match="comment body is required"):
                kb.add_comment(conn, tid, author="worker", body=blank)
        with pytest.raises(ValueError, match="comment author is required"):
            kb.add_comment(conn, tid, author="   ", body="valid note")


def test_add_comment_enforces_redaction_even_when_global_toggle_off(kanban_home, monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    value = _synthetic_secret()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="force mode")
        kb.add_comment(conn, tid, author="worker", body=f"https://eye.example.net/?t={value}&r=timr")
        stored = kb.list_comments(conn, tid)[0].body
        assert stored == "https://eye.example.net/?t=***&r=timr"
        assert value not in stored


def test_swarm_blackboard_ordinary_json_round_trips_unchanged(kanban_home):
    git_sha = secrets.token_hex(20)
    notion_page_id = secrets.token_hex(16)
    findings = {
        "video": "see https://youtu.be/x?t=30s",
        "doc": f"https://www.notion.so/workspace/Swarm-Notes-{notion_page_id}",
        "commit": git_sha,
        "paths": ["hermes_cli/kanban_db.py", "hermes_cli/kanban_swarm.py"],
        "counts": {"checked": 12, "passed": 12},
    }
    with kbc.connect_closing() as conn:
        created = create_swarm(
            conn,
            goal="Verify ordinary blackboard JSON survives comment redaction.",
            workers=[SwarmWorkerSpec(profile="researcher", title="Scan", body="Collect notes")],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="findings",
            value=findings,
        )

        board = latest_blackboard(conn, created.root_id)
        assert board["topology"]["root_id"] == created.root_id
        assert board["topology"]["worker_ids"] == created.worker_ids
        assert board["topology"]["goal"] == "Verify ordinary blackboard JSON survives comment redaction."
        assert board["findings"] == findings
        assert board["_authors"]["findings"] == "researcher"

        leaked = _synthetic_secret()
        post_blackboard_update(
            conn,
            created.root_id,
            author="researcher",
            key="credentials",
            value={"secret": leaked, "status": "checked"},
        )
        updated = latest_blackboard(conn, created.root_id)
        assert updated["credentials"] == {"secret": "***", "status": "checked"}
        assert leaked not in json.dumps(updated)


def test_kanban_comment_tool_end_to_end_redacts_before_show_replay(kanban_home):
    from tools import kanban_tools  # register handlers
    from tools.registry import registry

    bearer = _synthetic_secret()
    db_secret = _synthetic_secret()
    benign = "see https://youtu.be/x?t=30s"

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tool e2e comment redaction")

    for text in (
        f"https://eye.example.net/?t={bearer}&r=timr",
        f"DB_PASSWORD={db_secret}",
        benign,
    ):
        raw = registry.dispatch("kanban_comment", {"task_id": tid, "body": text})
        assert isinstance(raw, str)
        res = json.loads(raw)
        assert res.get("ok"), res

    with kbc.connect_closing() as conn:
        stored_bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert stored_bodies == [
        "https://eye.example.net/?t=***&r=timr",
        "DB_PASSWORD=***",
        benign,
    ]

    shown = registry.dispatch("kanban_show", {"task_id": tid})
    assert isinstance(shown, str)
    for secret in (bearer, bearer[:6], bearer[-4:], db_secret, db_secret[:6], db_secret[-4:]):
        assert secret not in shown
    assert "https://eye.example.net/?t=***&r=timr" in shown
    assert "DB_PASSWORD=***" in shown
    assert benign in shown
