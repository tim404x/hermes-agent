"""apexfix: monitor gate runs before admission, end to end against a real owner daemon.

An unchanged scheduled monitor tick through the new path must add no session row and no
``gateway.local_policy.v1`` receipt to the owner's state.db; the same tick through the old path (kill switch)
adds one of each. The measured numbers are written to ``$LANEC_E2E_OUT`` when set.
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

import pytest

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon

POLICY_GLOB = 'gateway.local_policy.v1:*'


def _store_counts(home):
    with closing(sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True)) as db:
        sessions = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
        receipts, size = db.execute('SELECT count(*), coalesce(sum(octet_length(value)), 0) FROM state_meta '
                                    'WHERE key GLOB ?', (POLICY_GLOB,)).fetchone()
    return {'sessions': sessions, 'receipts': receipts, 'receipt_bytes': size}


@pytest.fixture
def owner_daemon(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    (home / 'scripts').mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(user))
    monkeypatch.setenv('USERPROFILE', str(user))
    monkeypatch.delenv('HERMES_CRON_MONITOR_PREADMIT', raising=False)
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': 'http://127.0.0.1:9/v1'},
        'auxiliary': {'title_generation': {'enabled': False}},
        'platform_toolsets': {'gui': []}, 'terminal': {'cwd': str(home)},
    }))
    child_user = user / 'daemon'
    child_user.mkdir()
    env = child_env() | dict(HOME=str(child_user), USERPROFILE=str(child_user), HERMES_HOME=str(home),
                             PYTHONPATH=str(root), OPENAI_API_KEY='loopback-only', PYTHONUNBUFFERED='1')
    with daemon(root, home, env, barrier=False) as (proc, desc):
        yield home


def test_unchanged_tick_adds_no_session_and_no_receipt(owner_daemon, monkeypatch):
    import importlib

    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    from cron.jobs import create_job, get_job, update_job
    from cron.monitor import _run_monitor_source, hash_monitor_output
    from cron.monitor_preadmit import _update_state
    from cron.scheduler import run_job

    home = owner_daemon
    (home / 'scripts' / 'mon.sh').write_text("echo 'state A'\n")
    job = create_job(prompt='Summarize what changed', model='local-wire-stub', schedule='every 5m',
                     monitor_script='mon.sh', deliver='local')
    # The owner already saw 'state A' (a previous changed tick) and re-checked just now.
    ok, output = _run_monitor_source(job)
    assert ok, output
    update_job(job['id'], {'monitor_state': {'last_output_hash': hash_monitor_output(output),
                                             'last_changed_at': '2026-11-20T00:00:00+00:00'}})
    _update_state(job['id'], last_admit_at=time.time())

    def tick():
        fire = dict(get_job(job['id']), _scheduled_instant='2026-11-20T09:00:00+00:00',
                    execution_id=uuid.uuid4().hex)
        return run_job(fire, execution_id=fire['execution_id'])

    start = _store_counts(home)
    new_results = [tick() for _ in range(3)]
    after_new = _store_counts(home)

    monkeypatch.setenv('HERMES_CRON_MONITOR_PREADMIT', '0')
    old_results = [tick() for _ in range(3)]
    after_old = _store_counts(home)

    numbers = {
        'new_path_per_tick': {k: (after_new[k] - start[k]) / 3 for k in start},
        'old_path_per_tick': {k: (after_old[k] - after_new[k]) / 3 for k in start},
    }
    if os.environ.get('LANEC_E2E_OUT'):
        Path(os.environ['LANEC_E2E_OUT']).write_text(json.dumps(numbers, indent=1))
    assert after_new == start, numbers  # new path: nothing reserved
    assert after_old['sessions'] - after_new['sessions'] == 3, numbers  # old path: one session per tick
    assert after_old['receipts'] - after_new['receipts'] == 3, numbers  # ... and one receipt per tick
    strip = lambda r: (r[0], r[1].split('**Run Time:**')[0], r[2], r[3])  # noqa: E731 (timestamps differ)
    assert {strip(r) for r in new_results} == {strip(r) for r in old_results}
    assert all(r[0] is True and r[2] == '[SILENT]' and 'no_change' in r[1] for r in new_results + old_results)
