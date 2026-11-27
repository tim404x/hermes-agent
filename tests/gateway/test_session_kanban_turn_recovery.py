"""Owner-side kanban workers retry a failed turn in place, proven from the bound frame's claim."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import kanban_turn_recovery as rec
from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing

FAILED = {"failed": True, "failure_retryable": True, "failure_reason": "timeout",
          "error": "stream died", "messages": [{"role": "user", "content": "first"}]}


class _Agent:
    session_id = "s1"

    def __init__(self, results):
        self.results, self.calls, self.authors = list(results), [], []
        self._session_db = self

    def get_messages_as_conversation(self, _sid):
        return [{"role": "user", "content": "from-store"}]

    def run_conversation(self, message, conversation_history=None, turn_author=None):
        self.calls.append((message, conversation_history))
        self.authors.append(turn_author)
        return self.results.pop(0)


def _claimed_frame(tmp_path, monkeypatch, *, expired=False, goal_mode=False, skills=(), review=False):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TURN_RECOVERY", raising=False)
    # The owner/gateway process carries a FOREIGN (stale) env carrier: the proof must ignore it.
    for key, value in (("HERMES_KANBAN_TASK", "t_foreign"), ("HERMES_KANBAN_DB", str(tmp_path / "nope.db")),
                       ("HERMES_KANBAN_RUN_ID", "999"), ("HERMES_KANBAN_CLAIM_LOCK", "foreign")):
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(rec, "RECOVERY_DELAYS_SECONDS", (0.0,))
    kb.init_db()
    path = kb.kanban_db_path()
    with connect_closing(path) as conn:
        task_id = kb.create_task(conn, title="recover", assignee="default", skills=list(skills), workspace_path=str(tmp_path))
        if review:
            assert kb.request_review(conn, task_id, reviewer='reviewer')
        claim = kb.claim_review_task if review else kb.claim_task
        task = claim(conn, task_id, claimer="private-host:owner")
        assert task is not None
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "worker_bound", {"pid": os.getpid(), "claim_lock": task.claim_lock},
                             run_id=task.current_run_id)
            if expired:  # lease lapsed but not yet reaped: status/run/lock all still match
                conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task_id,))
                conn.execute("UPDATE task_runs SET claim_expires = 1 WHERE id = ?", (task.current_run_id,))
    context = {"db": str(path), "task_id": task_id, "run_id": task.current_run_id, "claim_lock": task.claim_lock,
               "skills": [], "context": "", "goal_mode": goal_mode, "goal_text": "g", "goal_max_turns": 2}
    return path, context, {"text": "work", "policy": {"kanban_json": json.dumps(context)}}


def test_missing_preloads_record_a_deterministic_worker_result(tmp_path, monkeypatch):
    path, context, frame = _claimed_frame(tmp_path, monkeypatch)
    monkeypatch.setattr("agent.skill_commands.build_preloaded_skills_prompt", lambda *_args, **_kwargs: ("", [], ["missing"]))
    with pytest.raises(ValueError, match="^kanban_skills_unavailable$") as exc:
        session_kanban.run_worker_turns(_Agent([]), frame, [])
    assert exc.value.missing == ["missing"]
    result = session_kanban.worker_result(path, context)
    assert (result["exit_code"], result["error"], result["last_output"]) == (1, "unknown_skill", "Unknown skill(s): missing")


@pytest.mark.parametrize('review,copies', [(True, 0), (True, 2), (True, 1), (False, 0)])
def test_only_review_claim_requires_an_unambiguous_reviewer(tmp_path, monkeypatch, review, copies):
    from gateway.session_contract import SessionActor

    for dirname, name in [('card', 'card'), *((f'copy{idx}', 'sdlc-review') for idx in range(copies))]:
        skill = tmp_path / 'skills' / dirname / 'SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text(f'---\nname: {name}\ndescription: testing\n---\nBody of {dirname}')
    names = ['card', 'absent-optional'] if review else ['card', 'sdlc-review']
    if copies == 2:
        names.append('copy0')
    path, context, frame = _claimed_frame(tmp_path, monkeypatch, review=review, skills=names)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda _: str(tmp_path))
    actor = SessionActor('owner', str(tmp_path), frozenset({'session:create'}), 'transport')
    connection = SimpleNamespace(native_owner=True, actor=actor, authority=SimpleNamespace(profile_id=str(tmp_path)))
    params = {key: context[key] for key in ('db', 'task_id', 'run_id', 'claim_lock')}
    policy, _ = session_kanban.build_kanban_policy(connection, dict(params, board='default'),
                                                  {'model': {'default': 'test', 'provider': 'custom'}})
    frame['policy'].update(source=policy.source, kanban_json=policy.kanban_json)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_kanban, '_watch_submitter', lambda *_: None)
    with patch.dict(os.environ):
        session_kanban.bind_worker_context(frame)
    agent = _Agent([{'completed': True}])
    if review and copies != 1:
        with pytest.raises(session_kanban.KanbanSkillsUnavailable) as exc:
            session_kanban._run_worker_turns(agent, frame, [])
        if copies == 2:
            assert 'copy0' not in exc.value.missing
            assert any(label.startswith('Ambiguous skill name sdlc-review:') for label in exc.value.missing)
        result = session_kanban.worker_result(path, context)
        assert result['error'] == 'unknown_skill' and 'sdlc-review' in result['last_output']
        assert not agent.calls
    else:
        session_kanban._run_worker_turns(agent, frame, [])
        assert 'Body of card' in agent.calls[0][0]
        if review:
            assert 'Body of copy0' in agent.calls[0][0]


def test_failed_turn_retries_in_place_from_frame_carrier(tmp_path, monkeypatch):
    path, context, frame = _claimed_frame(tmp_path, monkeypatch)
    agent = _Agent([dict(FAILED), {"completed": True, "final_response": "done"}])

    result = session_kanban.run_worker_turns(agent, frame, [])

    assert result["final_response"] == "done" and len(agent.calls) == 2
    nudge, history = agent.calls[1]
    assert context["task_id"] in nudge and "t_foreign" not in nudge
    assert history == FAILED["messages"]  # same session context, not a cold restart
    assert session_kanban.worker_exit_code(path, context) == 0
    # Both the first turn and the in-place retry are the dispatcher's, never the operator's (#137406).
    assert agent.authors == [{"id": "kanban:dispatcher", "name": "Kanban dispatcher", "is_bot": True}] * 2


def test_unprovable_claim_denies_retry_and_goal_continuation(tmp_path, monkeypatch):
    path, context, frame = _claimed_frame(tmp_path, monkeypatch, expired=True, goal_mode=True)
    goal_calls = []
    monkeypatch.setattr("hermes_cli.goals.run_kanban_goal_loop", lambda **kw: goal_calls.append(kw))
    agent = _Agent([dict(FAILED)])

    result = session_kanban.run_worker_turns(agent, frame, [])

    assert result["failed"] and len(agent.calls) == 1 and goal_calls == []
    assert session_kanban.worker_exit_code(path, context) == kb.KANBAN_RATE_LIMIT_EXIT_CODE
