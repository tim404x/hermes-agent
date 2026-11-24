"""Warn from the process-wide live registry, not from the size of one spawn request."""
import logging

from tools import delegate_tool_registry as registry


def test_many_small_delegations_warn_once_per_pressure_episode(monkeypatch, caplog):
    monkeypatch.setattr(registry, '_active_subagents', {})
    monkeypatch.setattr(registry, '_recent_subagents', {})
    monkeypatch.setattr(registry, '_pressure_warned', False, raising=False)
    caplog.set_level(logging.WARNING, logger=registry.logger.name)

    def add(i):
        registry._register_subagent({'subagent_id': f'child-{i}', 'owner_session_id': f'parent-{i%4}',
                                     'goal': 'private-goal-do-not-log'})

    def notes():
        return [r.getMessage() for r in caplog.records if r.name == registry.logger.name]

    for i in range(11):
        add(i)
    assert not notes()
    for i in range(11, 16):
        add(i)
    registry._unregister_subagent('child-12')
    add(12)
    assert len(notes()) == 1 and '12 live in-process subagents' in notes()[0] and 'Kanban' in notes()[0]
    assert 'private-goal' not in notes()[0]
    for i in range(16):
        registry._unregister_subagent(f'child-{i}')
    for i in range(12):
        add(i)
    assert len(notes()) == 2
