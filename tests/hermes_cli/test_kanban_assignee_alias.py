"""Tests for Kanban assignee alias resolution (hermes/neo/display names -> profile folder id)."""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_assignee as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_decompose as decomp
from tools import kanban_tools as kt


def _make_profile(home: Path, name: str, *, display_name: str | None = None) -> Path:
    pdir = home if name == "default" else home / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    if display_name is not None:
        (pdir / "profile.yaml").write_text(f"display_name: {display_name}\n", encoding="utf-8")
    else:
        (pdir / "config.yaml").write_text("{}\n", encoding="utf-8")
    return pdir


@pytest.fixture()
def alias_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with default (display_name Neo), atlas (Atlas Growth Lead), apex."""
    home = tmp_path / ".hermes"
    _make_profile(home, "default", display_name="Neo")
    _make_profile(home, "atlas", display_name="Atlas Growth Lead")
    _make_profile(home, "apex", display_name="Apex")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_SESSION_ID",
        "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _fake_spawn(*args, **kwargs) -> int:
    return 42424


@pytest.mark.parametrize("raw_assignee", ["hermes", "neo", "Neo"])
def test_tool_kanban_create_resolves_default_aliases_and_dispatcher_spawns(alias_env, raw_assignee):
    out = json.loads(kt._handle_create({"title": f"task for {raw_assignee}", "assignee": raw_assignee}))
    assert out.get("ok") is True, out
    tid = out["task_id"]

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.assignee == "default"
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert tid not in res.skipped_nonspawnable
    assert any(s[0] == tid and s[1] == "default" for s in res.spawned), res


def test_tool_kanban_create_resolves_display_name_slug(alias_env):
    out = json.loads(kt._handle_create({"title": "growth task", "assignee": "atlas-growth-lead"}))
    assert out.get("ok") is True, out
    tid = out["task_id"]

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.assignee == "atlas"
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert tid not in res.skipped_nonspawnable
    assert any(s[0] == tid and s[1] == "atlas" for s in res.spawned), res


def test_tool_kanban_create_rejects_unknown_profile_without_writing(alias_env):
    out = json.loads(kt._handle_create({"title": "orphan task", "assignee": "nobody"}))
    assert "error" in out, out
    assert "nobody" in out["error"]
    assert "default" in out["error"] and "atlas" in out["error"] and "apex" in out["error"]

    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def _cli_create_args(
    title: str,
    assignee: str | None,
    *,
    as_json: bool = False,
    idempotency_key: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        title=title, body=None, body_file=None, assignee=assignee, created_by=None,
        workspace="scratch", branch=None, project=None, tenant=None, priority=0,
        parent=None, triage=False, idempotency_key=idempotency_key, max_runtime=None, skills=None,
        max_retries=None, model_override=None, provider_override=None, goal_mode=False,
        goal_max_turns=None, completion_contract=None, resource_class=None,
        write_scope=None, read_only=False, initial_status="running", json=as_json,
    )


def _fake_aux_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def test_cli_create_unknown_stores_with_warning_and_alias_resolves_with_note(alias_env, capsys):
    rc = kc._cmd_create(_cli_create_args("cli unknown", "nobody", as_json=True))
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["assignee"] == "nobody"
    assert "warning:" in captured.err
    assert "'nobody'" in captured.err
    assert "not an installed profile" in captured.err

    rc2 = kc._cmd_create(_cli_create_args("cli alias", "hermes", as_json=True))
    assert rc2 == 0
    captured2 = capsys.readouterr()
    payload2 = json.loads(captured2.out)
    assert payload2["assignee"] == "default"
    assert "note:" in captured2.err
    assert "'hermes'" in captured2.err and "'default'" in captured2.err


def test_assign_and_reassign_resolve_alias_and_cli_warns(alias_env, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="move me", assignee="apex")
        assert kb.assign_task(conn, tid, "neo") is True
        assert kb.get_task(conn, tid).assignee == "default"

        assert kb.reassign_task(conn, tid, "atlas-growth-lead") is True
        assert kb.get_task(conn, tid).assignee == "atlas"

        assert kb.reassign_task(conn, tid, "Neo") is True
        assert kb.get_task(conn, tid).assignee == "default"

    rc = kc._cmd_assign(argparse.Namespace(task_id=tid, profile="nobody"))
    assert rc == 0
    cap = capsys.readouterr()
    assert "warning:" in cap.err and "'nobody'" in cap.err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "nobody"

    rc2 = kc._cmd_reassign(argparse.Namespace(task_id=tid, profile="hermes", reclaim=False, reason=None))
    assert rc2 == 0
    cap2 = capsys.readouterr()
    assert "note:" in cap2.err and "'hermes'" in cap2.err and "'default'" in cap2.err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).assignee == "default"


def test_request_review_tool_accepts_hermes_and_neo_aliases(alias_env, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="review me", assignee="atlas")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    out = json.loads(kt._handle_request_review({"summary": "ready for review", "reviewer": "hermes"}))
    assert out.get("ok") is True, out

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "review"
        assert task.assignee == "default"


def test_real_hermes_profile_wins_over_alias(alias_env):
    _make_profile(alias_env, "hermes")
    stored, is_installed = kb.resolve_assignee("hermes")
    assert (stored, is_installed) == ("hermes", True)

    out = json.loads(kt._handle_create({"title": "for real hermes", "assignee": "hermes"}))
    assert out.get("ok") is True, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, out["task_id"]).assignee == "hermes"


def test_real_neo_profile_wins_over_display_name_alias(alias_env):
    _make_profile(alias_env, "neo")
    stored, is_installed = kb.resolve_assignee("neo")
    assert (stored, is_installed) == ("neo", True)


def test_installed_name_fast_path_skips_alias_lookup(alias_env, monkeypatch):
    from tools import bot_mode_probe

    def _boom(*args, **kwargs):
        raise AssertionError("fast path must not read aliases")

    monkeypatch.setattr(bot_mode_probe, "local_alias_map", _boom)

    assert kb.resolve_assignee("atlas") == ("atlas", True)
    assert kb.resolve_assignee("ATLAS") == ("atlas", True)
    assert kb.resolve_assignee("default") == ("default", True)


def test_resolve_assignee_none_and_non_string_and_resolver_exception(alias_env, monkeypatch):
    assert kb.resolve_assignee(None) == (None, False)
    with pytest.raises(ValueError):
        kb.resolve_assignee(123)  # type: ignore[arg-type]

    from tools import bot_mode_dm

    def _boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(bot_mode_dm, "_resolve_local_name", _boom)
    assert kb.resolve_assignee("Neo") == ("neo", False)


def test_list_tasks_filters_by_alias(alias_env):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="for default", assignee="hermes")
        found = kb.list_tasks(conn, assignee="neo")
    assert [t.id for t in found] == [tid]


def test_list_tasks_matches_legacy_alias_and_resolved_rows(alias_env):
    """Pre-upgrade rows stored literally as 'hermes' remain visible alongside 'default'."""
    with kbc.connect_closing() as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t_legacy_hermes", "legacy card", None, "hermes", "ready", 0, "user", int(time.time())),
        )
        conn.commit()
        new_default_id = kb.create_task(conn, title="new default card", assignee="hermes")
        kb.create_task(conn, title="atlas card", assignee="atlas")

        found_hermes = {t.id for t in kb.list_tasks(conn, assignee="hermes")}
        found_default = {t.id for t in kb.list_tasks(conn, assignee="default")}

    assert found_hermes == {"t_legacy_hermes", new_default_id}
    assert found_default == {new_default_id}


def test_decompose_single_task_resolves_alias_with_different_fallback(alias_env):
    """Single-task promotion resolves 'atlas-growth-lead' -> 'atlas' instead of fallback 'apex'."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="rough growth idea", triage=True)

    llm_payload = json.dumps({
        "fanout": False,
        "rationale": "single growth task",
        "title": "Audit growth funnel",
        "body": "Check activation dropoff.",
        "assignee": "atlas-growth-lead",
    })
    with (
        patch("agent.auxiliary_client.call_llm", return_value=_fake_aux_response(llm_payload)),
        patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "apex", "orchestrator_profile": "apex"}},
        ),
    ):
        outcome = decomp.decompose_task(tid, author="tester")

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "atlas"


def test_decompose_fanout_resolves_aliases_with_different_fallback(alias_env):
    """Fan-out resolves aliases ('atlas-growth-lead'->'atlas', 'Neo'->'default') while unknown falls back to 'apex'."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="multi-lane launch", assignee="apex", triage=True)

    llm_payload = json.dumps({
        "fanout": True,
        "rationale": "split across growth, default, and fallback",
        "tasks": [
            {"title": "growth campaign", "body": "launch ads", "assignee": "atlas-growth-lead", "parents": []},
            {"title": "draft comms", "body": "write update", "assignee": "Neo", "parents": []},
            {"title": "unmatched lane", "body": "fallback check", "assignee": "nonexistent-bot", "parents": []},
        ],
    })
    with (
        patch("agent.auxiliary_client.call_llm", return_value=_fake_aux_response(llm_payload)),
        patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "apex", "orchestrator_profile": "apex"}},
        ),
    ):
        outcome = decomp.decompose_task(tid, author="tester")

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 3
    with kbc.connect_closing() as conn:
        tasks = [kb.get_task(conn, cid) for cid in outcome.child_ids]
    assert [t.assignee for t in tasks if t is not None] == ["atlas", "default", "apex"]


@pytest.mark.parametrize("bad_assignee", [["Neo"], 123, {"name": "Neo"}, "   ", ""])
def test_tool_kanban_create_rejects_non_string_and_blank_assignee(alias_env, bad_assignee):
    out = json.loads(kt._handle_create({"title": "bad assignee", "assignee": bad_assignee}))
    assert "error" in out, out
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_cli_create_idempotent_retry_warns_for_returned_card_not_request(alias_env, capsys):
    # First create succeeds on installed 'atlas' with no warning.
    rc1 = kc._cmd_create(_cli_create_args("first", "atlas", as_json=True, idempotency_key="idem-atlas"))
    assert rc1 == 0
    cap1 = capsys.readouterr()
    tid1 = json.loads(cap1.out)["id"]
    assert cap1.err == ""

    # Retry same key with unknown 'nobody' returns existing 'atlas' card and must NOT warn about 'nobody'.
    rc2 = kc._cmd_create(_cli_create_args("retry unknown", "nobody", as_json=True, idempotency_key="idem-atlas"))
    assert rc2 == 0
    cap2 = capsys.readouterr()
    payload2 = json.loads(cap2.out)
    assert payload2["id"] == tid1
    assert payload2["assignee"] == "atlas"
    assert "nobody" not in cap2.err
    assert "warning:" not in cap2.err
    assert "note:" not in cap2.err

    # Retry same key with alias 'hermes' suppresses the alias note because the card was not assigned by this call.
    rc3 = kc._cmd_create(_cli_create_args("retry alias", "hermes", as_json=True, idempotency_key="idem-atlas"))
    assert rc3 == 0
    cap3 = capsys.readouterr()
    assert json.loads(cap3.out)["id"] == tid1
    assert "note:" not in cap3.err
    assert "warning:" not in cap3.err

    # Reverse case: existing card belongs to uninstalled 'ghost'; retry with 'atlas' still warns about 'ghost'.
    rc4 = kc._cmd_create(_cli_create_args("ghost card", "ghost", as_json=True, idempotency_key="idem-ghost"))
    assert rc4 == 0
    capsys.readouterr()
    rc5 = kc._cmd_create(_cli_create_args("retry ghost", "atlas", as_json=True, idempotency_key="idem-ghost"))
    assert rc5 == 0
    cap5 = capsys.readouterr()
    assert json.loads(cap5.out)["assignee"] == "ghost"
    assert "warning:" in cap5.err and "'ghost'" in cap5.err
    assert "'atlas'" not in cap5.err


def test_cli_create_succeeds_without_roster_when_list_profile_names_raises(alias_env, monkeypatch, capsys):
    from hermes_cli import profiles as profiles_mod

    def _boom():
        raise OSError("profiles dir unreadable")

    monkeypatch.setattr(profiles_mod, "list_profile_names", _boom)

    rc = kc._cmd_create(_cli_create_args("survive roster error", "nobody", as_json=True))
    assert rc == 0
    cap = capsys.readouterr()
    payload = json.loads(cap.out)
    assert payload["assignee"] == "nobody"
    assert "warning:" in cap.err
    assert "'nobody'" in cap.err
    assert "not an installed profile" in cap.err
    assert "(installed:" not in cap.err

    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, payload["id"]) is not None


def test_resolve_assignee_logs_exceptions_on_fallback(alias_env, monkeypatch, caplog):
    from hermes_cli import profiles as profiles_mod
    from tools import bot_mode_dm

    caplog.set_level(logging.DEBUG, logger=ka.__name__)
    with monkeypatch.context() as mp:
        mp.setattr(profiles_mod, "list_profile_names", lambda: (_ for _ in ()).throw(RuntimeError("list boom")))
        assert kb.resolve_assignee("Neo") == ("neo", False)
    assert any("failed to list profiles" in r.message and r.exc_info for r in caplog.records)

    caplog.clear()
    with monkeypatch.context() as mp:
        mp.setattr(bot_mode_dm, "_resolve_local_name", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("dm boom")))
        assert kb.resolve_assignee("Neo") == ("neo", False)
    assert any("alias lookup failed" in r.message and r.exc_info for r in caplog.records)


def test_tool_create_and_request_review_do_not_eagerly_scan_roster_on_success(alias_env, monkeypatch):
    from hermes_cli import profiles as profiles_mod

    orig_list = profiles_mod.list_profile_names
    orig_resolve = ka.resolve_assignee
    list_calls = 0
    resolve_calls = 0

    def _counting_list():
        nonlocal list_calls
        list_calls += 1
        return orig_list()

    def _counting_resolve(name):
        nonlocal resolve_calls
        resolve_calls += 1
        return orig_resolve(name)

    monkeypatch.setattr(profiles_mod, "list_profile_names", _counting_list)
    monkeypatch.setattr(ka, "resolve_assignee", _counting_resolve)
    monkeypatch.setattr(kb, "resolve_assignee", _counting_resolve)

    # Success path: zero extra list_profile_names scans outside resolve_assignee.
    out = json.loads(kt._handle_create({"title": "lazy roster create", "assignee": "atlas"}))
    assert out.get("ok") is True, out
    assert list_calls == resolve_calls

    # Rejection path: computes installed list once for the error message.
    list_calls = resolve_calls = 0
    bad_out = json.loads(kt._handle_create({"title": "bad", "assignee": "nobody"}))
    assert "error" in bad_out
    assert list_calls == resolve_calls + 1

    tid = out["task_id"]
    with kbc.connect_closing() as conn:
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    list_calls = resolve_calls = 0
    rev_out = json.loads(kt._handle_request_review({"summary": "ready", "reviewer": "hermes"}))
    assert rev_out.get("ok") is True, rev_out
    assert list_calls == resolve_calls
