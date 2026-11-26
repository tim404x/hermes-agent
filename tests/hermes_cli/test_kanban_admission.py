import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatcher
from hermes_cli import projects_db


TABLE = '| # | Task | Card | Goal | Budget | Outcome |\n|---|---|---|---|---|---|\n'
RANKED = '## Ranked (current work)\n' + TABLE


def rank(root, *task_ids):
    rows = [f'| {index} | experiment | {task_id} | test | low | evaluate |' for index, task_id in enumerate(task_ids, 1)]
    (root / 'PRIORITIES.md').write_text(RANKED + '\n'.join(rows) + '\n', encoding='utf-8')


def card(conn, workspace, resource='cpu', writes=(), **kwargs):
    return kb.create_task(conn, title='experiment', workspace_kind='dir', workspace_path=str(workspace),
                          resource_class=resource, write_scopes=None if writes is None else list(writes), **kwargs)


def assert_waiting(conn, task_id, reason, status='ready'):
    task = kb.get_task(conn, task_id)
    assert (task.status, task.claim_lock, task.current_run_id, task.admission_reason) == (status, None, None, reason)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    root = tmp_path / 'project'
    root.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    board = kb.init_db(board='default')
    registry = home / 'kanban' / 'guarded-projects.json'
    registry.parent.mkdir(exist_ok=True)
    registry.write_text(json.dumps({'lab': {
        'workspace_root': str(root), 'board_db': str(board),
        'priorities_file': str(root / 'PRIORITIES.md'),
    }}), encoding='utf-8')
    return root, board


def test_unranked_shared_dir_never_claimed(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        task_id = kb.create_task(conn, title='shared dir', workspace_kind='dir', workspace_path=str(root))
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'priorities_unavailable')
        assert not kb.list_runs(conn, task_id)


def test_only_exact_ids_in_numbered_card_cells(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        task_id = card(conn, root)
        noise = (f'| 1 | {task_id} | t_00000000 | {task_id} | {task_id} | {task_id} |\n'
                 f'| 2 | test | team cards incl. {task_id} | why | cap | stop |\n'
                 f'| later | test | {task_id} | why | cap | stop |\n'
                 f'| 3 | test | {task_id}a | why | cap | stop |\n'
                 f'| 4 | test | {task_id}, team cards | why | cap | stop |\n'
                 f'## Stopped / history\n{TABLE}| 1 | test | {task_id} | why | cap | stop |\n')
        (root / 'PRIORITIES.md').write_text(RANKED + noise)
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'not_ranked')
        rank(root, f't_00000000, {task_id}')
        assert kb.claim_task(conn, task_id).admission_reason is None


def test_narrow_rank_table_dividers_do_not_skip_prose_or_malformed_cells(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        task_id = card(conn, root)
        table = '| # | Card |\n{}\n| 1 | ' + task_id + ' |\n'
        priorities = root / 'PRIORITIES.md'
        heading = '## Ranked\n\n---\n\n'
        for body in (table.format('| :- | :: |'), 'History:\n' + table.format('| :- | -: |')):
            priorities.write_text(heading + body)
            assert kb.claim_task(conn, task_id) is None
            assert_waiting(conn, task_id, 'not_ranked')
        priorities.write_text(heading + table.format('| :- | -: |'))
        assert kb.claim_task(conn, task_id)


def test_queued_cards_can_be_populated_but_active_declarations_are_fixed(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        task_id = card(conn, root, None, None)
        rank(root, task_id)
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'missing_resource_class')
        assert kb.edit_task(conn, task_id, resource_class='data')
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'missing_write_scopes')
        assert kb.edit_task(conn, task_id, write_scopes=[])
        claimed = kb.claim_task(conn, task_id)
        assert (claimed.resource_class, claimed.write_scopes) == ('data', [])
        with pytest.raises(ValueError, match='requeue'):
            kb.edit_task(conn, task_id, resource_class='cpu')


@pytest.mark.parametrize(('first', 'second'), [('models', 'models/run'), ('models/run', 'models'), ('.', 'models')])
def test_overlap_is_component_based_and_read_only_has_no_write_lock(fleet, first, second):
    root, board = fleet
    alias = root.parent / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with kbc.connect_closing(board) as conn:
        owner = card(conn, root, writes=[first])
        overlap = card(conn, alias, writes=[str(alias / second)])
        reader = card(conn, alias)
        sibling = card(conn, root, writes=['models-extra'])
        rank(root, owner, overlap, reader, sibling)
        assert kb.claim_task(conn, owner)
        assert kb.claim_task(conn, overlap) is None
        assert_waiting(conn, overlap, 'write_scope_busy')
        assert kb.claim_task(conn, reader)
        assert bool(kb.claim_task(conn, sibling)) == (first != '.')
        assert kb.reclaim_task(conn, owner)
        assert kb.claim_task(conn, overlap)


def test_claim_snapshot_keeps_original_directory_reserved_if_alias_moves(fleet):
    root, board = fleet
    alias = root / 'alias'
    alias.symlink_to(root / 'original', target_is_directory=True)
    with kbc.connect_closing(board) as conn:
        owner = card(conn, root, writes=['alias'])
        next_task = card(conn, root, writes=['original/out'])
        rank(root, owner, next_task)
        assert kb.claim_task(conn, owner)
        alias.unlink()
        alias.symlink_to(root / 'replacement', target_is_directory=True)
        assert kb.claim_task(conn, next_task) is None
        assert_waiting(conn, next_task, 'write_scope_busy')


@pytest.mark.parametrize('scope', ['../project/within', '/outside', 'escape/out', '**/out'])
def test_guard_refuses_traversal_escape_and_patterns(fleet, scope):
    root, board = fleet
    (root / 'escape').symlink_to(root.parent, target_is_directory=True)
    with kbc.connect_closing(board) as conn:
        task_id = card(conn, root, writes=[scope])
        rank(root, task_id)
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'invalid_write_scope')


def test_workspace_alias_cannot_escape_or_disguise_a_different_board(fleet):
    root, board = fleet
    alias = root.parent / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    (root / 'escape').symlink_to(root.parent, target_is_directory=True)
    with kbc.connect_closing(board) as conn:
        for workspace in (alias / 'escape' / 'outside', root / '..' / 'outside'):
            task_id = card(conn, workspace)
            assert kb.claim_task(conn, task_id) is None
            assert_waiting(conn, task_id, 'workspace_outside_project')
    with kbc.connect_closing(root.parent / 'other.db') as other:
        task_id = card(other, alias)
        rank(root, task_id)
        assert kb.claim_task(other, task_id, board='default') is None
        assert_waiting(other, task_id, 'wrong_project_board')
        control = card(other, root.with_name(root.name + '-unrelated'), None, None)
        assert kb.claim_task(other, control)


def test_stored_project_identity_also_matches_outside_workspace(fleet):
    root, board = fleet
    (root / '.git').mkdir()
    with projects_db.connect_closing() as projects:
        project_id = projects_db.create_project(projects, name='Lab', folders=[str(root)])
    registry = kb.kanban_home() / 'kanban' / 'guarded-projects.json'
    config = json.loads(registry.read_text())
    config['lab']['project_id'] = project_id
    registry.write_text(json.dumps(config))
    with kbc.connect_closing(board) as conn:
        external = card(conn, root.parent / 'elsewhere', project_id=project_id)
        assert kb.claim_task(conn, external) is None
        assert_waiting(conn, external, 'workspace_outside_project')
        local = kb.create_task(conn, title='linked worktree', project_id=project_id, resource_class='cpu', write_scopes=[])
        rank(root, local)
        assert kb.claim_task(conn, local)


def test_worktree_materialization_cannot_follow_an_escaping_anchor(fleet):
    root, board = fleet
    (root / '.git').mkdir()
    (root / '.worktrees').symlink_to(root.parent / 'outside', target_is_directory=True)
    with kbc.connect_closing(board) as conn:
        task_id = kb.create_task(conn, title='anchored', workspace_kind='worktree', workspace_path=str(root),
                                 resource_class='cpu', write_scopes=['models'])
        rank(root, task_id)
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'workspace_outside_project')


@pytest.mark.parametrize(('resource', 'cap', 'reason'), [('gpu', 3, 'gpu_lane_cap'), ('data', 1, 'data_lane_cap'), ('cpu', 6, 'project_card_cap')])
def test_project_caps_include_review_and_release_on_completion(fleet, resource, cap, reason):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        review, *others, waiting = [card(conn, root, resource) for _ in range(cap + 1)]
        rank(root, review, *others, waiting)
        assert kb.request_review(conn, review, reviewer='reader')
        for task_id in others:
            assert kb.claim_task(conn, task_id)
        assert kb.claim_task(conn, waiting) is None
        assert_waiting(conn, waiting, reason)
        assert kb.request_review(conn, waiting, force=True, with_reason=True) == (False, reason)
        assert_waiting(conn, waiting, reason)
        assert kb.claim_review_task(conn, review)
        assert kb.complete_task(conn, review, result='reviewed')
        assert kb.claim_task(conn, waiting)


def test_legacy_running_rows_are_not_free_lanes(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        legacy = card(conn, root, None, None)
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (legacy,))
        data = card(conn, root, 'data')
        writer = card(conn, root, writes=['isolated'])
        rank(root, data, writer)
        assert kb.claim_task(conn, data) is None
        assert_waiting(conn, data, 'data_lane_cap')
        assert kb.claim_task(conn, writer) is None
        assert_waiting(conn, writer, 'write_scope_busy')
        assert kb.reclaim_task(conn, legacy)
        assert kb.claim_task(conn, writer)
        assert kb.claim_task(conn, data)


@pytest.mark.parametrize('handoff', [False, True])
def test_independent_connections_cannot_both_take_last_gpu_slot(fleet, handoff):
    root, board = fleet
    barrier = Barrier(2, timeout=15)

    def compete(task_id):
        with kbc.connect_closing(board) as contender:
            barrier.wait()
            if handoff and task_id == contenders[0]:
                return kb.request_review(contender, task_id, reviewer='reader')
            return kb.claim_task(contender, task_id) is not None

    with kbc.connect_closing(board) as conn:
        first, second, *contenders = [card(conn, root, 'gpu') for _ in range(4)]
        rank(root, first, second, *contenders)
        assert kb.claim_task(conn, first) and kb.claim_task(conn, second)
        with ThreadPoolExecutor(2) as pool:
            verdicts = list(pool.map(compete, contenders))
        assert sorted(verdicts) == [False, True]
        loser = contenders[verdicts.index(False)]
        assert_waiting(conn, loser, 'gpu_lane_cap')
        assert not kb.list_runs(conn, loser)
        assert kb.reclaim_task(conn, first)
        assert kb.claim_task(conn, loser)


def test_named_board_cannot_be_bypassed_by_pin_or_foreign_profile_home(fleet, monkeypatch):
    root, default_board = fleet
    named_board = Path(kb.create_board('research')['db_path'])
    registry = kb.kanban_home() / 'kanban' / 'guarded-projects.json'
    config = json.loads(registry.read_text())
    config['lab']['board_db'] = str(named_board)
    registry.write_text(json.dumps(config))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root.parent / 'other-fleet'))
    with kbc.connect_closing(default_board) as other, kbc.connect_closing(named_board) as authority:
        wrong = card(other, root)
        right = card(authority, root)
        rank(root, wrong, right)
        assert kb.claim_task(other, wrong, board='research') is None
        assert_waiting(other, wrong, 'wrong_project_board')
        assert kb.claim_task(authority, right)


def test_legacy_implicit_worktree_uses_actual_boards_anchor(fleet):
    root, _ = fleet
    named = kb.create_board('other', default_workdir=str(root))
    with kbc.connect_closing(Path(named['db_path'])) as conn:
        task_id = kb.create_task(conn, title='legacy worktree', workspace_kind='worktree')
        assert kb.get_task(conn, task_id).workspace_path is None
        assert kb.claim_task(conn, task_id) is None
        assert_waiting(conn, task_id, 'wrong_project_board')


@pytest.mark.parametrize('claim_board', [None, 'guarded'])
def test_implicit_worktree_ignores_unrelated_board_anchor(fleet, claim_board):
    root, _ = fleet
    kb.create_board('guarded', default_workdir=str(root))
    other = kb.create_board('other')
    with kbc.connect_closing(Path(other['db_path'])) as conn:
        task_id = kb.create_task(conn, title='unrelated worktree', workspace_kind='worktree', board='other')
        assert kb.get_task(conn, task_id).workspace_path is None
        if claim_board is None:
            kb.set_current_board('guarded')
        assert kb.claim_task(conn, task_id, board=claim_board)


@pytest.mark.parametrize(('guarded', 'other'), [('default', 'other'), ('other', 'default')])
def test_implicit_scratch_matches_only_its_own_boards_root(fleet, guarded, other):
    root, _ = fleet
    kb.create_board('other')
    scratch = kb.workspaces_root(board=guarded)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.symlink_to(root, target_is_directory=True)
    with kbc.connect_closing(kb.kanban_db_path(guarded)) as conn:
        task_id = kb.create_task(conn, title='guarded scratch', workspace_kind='scratch')
        assert kb.claim_task(conn, task_id, board=other) is None
        reason = 'priorities_unavailable' if guarded == 'default' else 'wrong_project_board'
        assert_waiting(conn, task_id, reason)
    with kbc.connect_closing(kb.kanban_db_path(other)) as conn:
        task_id = kb.create_task(conn, title='unrelated scratch', workspace_kind='scratch')
        kb.set_current_board(guarded)
        assert kb.claim_task(conn, task_id)


def test_project_slots_are_not_shared_with_another_guarded_project(fleet):
    root, board = fleet
    second = root.with_name('other-project')
    second.mkdir()
    registry = kb.kanban_home() / 'kanban' / 'guarded-projects.json'
    config = json.loads(registry.read_text())
    config['other'] = dict(config['lab'], workspace_root=str(second), priorities_file=str(second / 'PRIORITIES.md'))
    registry.write_text(json.dumps(config))
    with kbc.connect_closing(board) as conn:
        first = card(conn, root, 'data', ['.'])
        other = card(conn, second, 'data', ['.'])
        rank(root, first)
        rank(second, other)
        assert kb.claim_task(conn, first)
        assert kb.claim_task(conn, other)


def test_automatic_and_manual_review_resume_must_reacquire_capacity(fleet):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        review = card(conn, root, 'data')
        rival = card(conn, root, 'data')
        rank(root, review, rival)
        assert kb.request_review(conn, review, reviewer='reader')
        current = kb.claim_review_task(conn, review)
        assert kb.block_task(conn, review, reason='waiting', expected_run_id=current.current_run_id)
        assert kb.claim_task(conn, rival)
        assert not kb.unblock_task(conn, review)
        assert_waiting(conn, review, 'data_lane_cap', 'blocked')
        assert kb.reclaim_task(conn, rival)
        assert kb.unblock_task(conn, review)
        current = kb.claim_review_task(conn, review)
        parent = kb.create_task(conn, title='prerequisite')
        kb.link_tasks(conn, parent, review, expected_child_run_id=current.current_run_id)
        assert kb.block_task(conn, review, reason='needs prerequisite', kind='dependency', expected_run_id=current.current_run_id)
        assert kb.get_task(conn, review).status == 'todo'
        assert kb.claim_task(conn, rival)
        assert kb.complete_task(conn, parent, result='prepared')
        kb.recompute_ready(conn)
        assert_waiting(conn, review, 'data_lane_cap', 'todo')
        assert kb.reclaim_task(conn, rival)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, review).status == 'review'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_real_dispatch_stops_before_materialization_or_spawn(fleet, all_assignees_spawnable, lane):
    root, board = fleet
    target = root / 'not-created'
    with kbc.connect_closing(board) as conn:
        task_id = card(conn, target, assignee='reader')
        if lane == 'review':
            rank(root, task_id)
            assert kb.request_review(conn, task_id, reviewer='reader')
            (root / 'PRIORITIES.md').unlink()
        runs = kb.list_runs(conn, task_id)
        for _ in range(2):
            tick = dispatcher.dispatch_once(conn, spawn_fn=lambda *_args: pytest.fail('rejected worker spawned'))
            assert not tick.spawned
        assert not target.exists()
        assert_waiting(conn, task_id, 'priorities_unavailable', lane)
        assert kb.list_runs(conn, task_id) == runs


@pytest.mark.parametrize('max_spawn', [1, 3])
def test_rejected_review_returns_its_slot_to_untried_ready_work(fleet, all_assignees_spawnable, max_spawn):
    root, board = fleet
    with kbc.connect_closing(board) as conn:
        review = card(conn, root, assignee='writer')
        rank(root, review)
        assert kb.request_review(conn, review, reviewer='reader')
        rank(root)
        delayed = kb.create_task(conn, title='cooling down', assignee='worker', priority=10)
        conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                     "VALUES (?, 'worker', 'rate_limited', 'rate_limited', unixepoch(), unixepoch())", (delayed,))
        ready = [kb.create_task(conn, title='queued', assignee='worker', priority=-index)
                 for index in range(max_spawn + 1)]
        tick = dispatcher.dispatch_once(conn, max_spawn=max_spawn, spawn_fn=lambda *_args: None)
        assert [task_id for task_id, *_ in tick.spawned] == ready[:max_spawn]
        assert_waiting(conn, review, 'not_ranked', 'review')
        assert kb.get_task(conn, ready[-1]).status == 'ready'
        assert tick.respawn_guarded == [(delayed, 'rate_limit_cooldown')]
        assert [event.kind for event in kb.list_events(conn, delayed)].count('respawn_guarded') == 1
        for task_id in ready[:max_spawn]:
            assert kb.complete_task(conn, task_id, result='finished')
        rank(root, review)
        next_tick = dispatcher.dispatch_once(conn, max_spawn=1, spawn_fn=lambda *_args: None)
        assert [task_id for task_id, *_ in next_tick.spawned] == [review]
