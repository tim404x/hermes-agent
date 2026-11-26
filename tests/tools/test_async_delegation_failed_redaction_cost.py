import time
from unittest.mock import patch

from tests.tools import test_async_delegation_failed_surface as surface
from tools import async_delegation as ad
from tools import delegation_redaction as redaction
from tools.delegate_tool_auto_resume import waiting_on_resume

home = surface.home
_delegation = surface._delegation


def _old_failed_rows(rows, limit):
    failed = []
    for row in rows:
        task, result = row.task, redaction.redact_child_result(row.result)
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        if waiting_on_resume(result):
            continue
        if not tasks and str(row.state).lower() in ad._FAILED_TASK_STATES:
            tasks = [{"task_index": index, "error": result.get("error")} for index in task.get("task_indexes") or [0]]
        for entry in tasks:
            status = str(entry.get("status") or row.state or "").lower()
            if status not in ad._FAILED_TASK_STATES or waiting_on_resume(entry):
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({"delegation_id": row.delegation_id, "task_index": index, "status": status,
                           "goal": str((goals[index] if 0 <= index < len(goals) else goals[0]) or ""),
                           "error": str(error) if error else None, "dispatched_at": row.dispatched_at,
                           "completed_at": row.completed_at})
    return failed[:limit]


def test_selective_redaction_matches_full_result_projection(home):
    secret = "ghp_" + "a1Bc" * 10
    _delegation("done", status="completed", result={"summary": "complete " * 20000})
    _delegation("single", status="error", goal="report", result={})
    _delegation("batch", status="completed", is_batch=True, goals=["done", "timeout", "waiting"], result={
        "error": {"detail": secret}, "results": [
            {"status": "completed"}, {"status": "timeout", "task_index": 1, "error": ""},
            {"status": "error", "task_index": 2, "auto_resume": {}},
            {"status": "error", "waiting_to_resume": True}, {"status": "error", "unstarted_auto": True}]})
    _delegation("crash", status="stalled", is_batch=True, task_indexes=[1, 2], goals=["a", "b", "c"],
                result={"results": [], "error": "stopped"})
    _delegation("waiting", status="error", result={"auto_resume": {}})
    with ad._ledger() as store:
        store.coordination.record_delegation_completion(delegation_id="single", state="error", completed_at=time.time(),
                                                       event={}, result={"error": f"error: {secret}"})
        rows = store.coordination.list_finished_delegations(origin_ui_session_id="ui-1")
    expected = _old_failed_rows(rows, 20)
    assert len(expected) == 4 and secret not in str(expected)
    assert ad.failed_delegations_for_session("ui-1") == expected


def test_poll_redacts_only_errors_it_returns(home):
    _delegation("done", status="completed", result={"summary": "verbose " * 1000})
    _delegation("batch", status="completed", ui="ui-2", result={"results": [
        {"status": "error", "error": "failed", "summary": "transcript"}, {"status": "completed", "summary": "ok"}]})
    with patch.object(ad, "redact_child_result", wraps=redaction.redact_child_result) as full, patch.object(
        ad, "redact_child_value", create=True, wraps=redaction.redact_child_value
    ) as value:
        assert ad.failed_delegations_for_session("ui-1") == []
        full.assert_not_called()
        value.assert_not_called()
        assert [row["error"] for row in ad.failed_delegations_for_session("ui-2")] == ["failed"]
        value.assert_called_once_with("failed", force=True)
        full.assert_not_called()
