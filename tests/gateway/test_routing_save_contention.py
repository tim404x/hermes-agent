"""End-to-end real gateway generation saves versus normal state.db traffic, including durable commit."""

import json
import statistics
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionSource, SessionStore
from persistence.sqlite import _holder
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_seed import seed_session


@pytest.mark.parametrize('mode', ['wal', 'delete'])
def test_gateway_10k_competing_writers(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    if mode == 'delete':
        monkeypatch.setattr(_holder, 'apply_wal_with_fallback', lambda conn, **_: conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0])
    store = SessionStore(tmp_path / 'sessions', GatewayConfig(write_sessions_json=False))
    try:
        store._ensure_loaded()
        db, holder = store.persistence, raw_holder(store.persistence)
        assert holder._conn.execute('PRAGMA journal_mode').fetchone()[0] == mode
        now = datetime.now(UTC)
        for index in range(10_000):
            key = f'agent:main:peer:{index}'
            store._entries[key] = SessionEntry(key, f's-{index}', now, now,
                origin=SessionSource(Platform.TELEGRAM, chat_id=str(index)),
                prompt_pin={'version': 1, 'context_prompt': 'p' * 4096, 'context_key': 'key', 'redact_pii': False})
        store._save_entries()
        for index in range(32):
            seed_session(db, f'admission-target-{index}', 'api_server')
        epoch = db.runtime.begin_epoch(instance_id='bench-owner')
        mutex = _holder.writer_mutex_until
        local = threading.local()
        holds, totals, blocks, writes, commits = (defaultdict(list) for _ in range(5))
        execute = holder._execute_write

        def record_body_end(callback, *args, **kwargs):
            def body(conn):
                result = callback(conn)
                local.body_end = time.perf_counter()
                return result
            return execute(body, *args, **kwargs)

        @contextmanager
        def timed_mutex(*args, **kwargs):
            operation = local.operation
            with mutex(*args, **kwargs):
                start = time.perf_counter()
                local.body_end = None
                changed = holder._conn.total_changes
                work = []
                holder._conn.set_progress_handler(lambda: work.append(1), 128)
                try:
                    yield
                finally:
                    holder._conn.set_progress_handler(None, 0)
                    blocks[operation].append(len(work))
                    writes[operation].append(holder._conn.total_changes - changed)
            holds[operation].append((time.perf_counter() - start) * 1000)
            if local.body_end is not None:
                commits[operation].append((time.perf_counter() - local.body_end) * 1000)

        def measure(name, func, *args, **kwargs):
            local.operation = name
            start = time.perf_counter()
            func(*args, **kwargs)
            totals[name].append((time.perf_counter() - start) * 1000)

        stop, running = threading.Event(), threading.Event()

        def traffic():
            index = 0
            while index < 200 and (not stop.is_set() or index < 20):
                measure('upsert', db.routing.upsert_routing_entry, 'direct', json.dumps({'session_id': f'direct-{index}'}))
                measure('admit', db.admissions.admit, epoch=epoch, principal_id='tester', session_id=f'admission-target-{index % 32}',
                        request_id=f'request-{index}', payload={'text': str(index)})
                measure('retire', db.routing.fence_retired_routes, [f'outside-{index}'], namespace='other')
                index += 1
                running.set()

        with monkeypatch.context() as measuring, ThreadPoolExecutor(max_workers=1) as workers:
            measuring.setattr(_holder, 'writer_mutex_until', timed_mutex)
            measuring.setattr(holder, '_execute_write', record_body_end)
            for version in range(5):
                measure('point control', db.routing.upsert_routing_entry, 'floor', json.dumps({'session_id': str(version)}))
            peer = workers.submit(traffic)
            try:
                assert running.wait(10) or peer.result()
                for version in range(20):
                    store._entries['agent:main:peer:2'].last_prompt_tokens = version + 1
                    store._entries['agent:main:peer:2'].session_id = f'new-{version}'
                    measure('save', store._save_entries)
            finally:
                stop.set()
                peer.result(timeout=20)
        for name in totals:
            print(f'{mode} {name} n={len(totals[name])} elapsed median/max ms '
                  f'{statistics.median(totals[name]):.3f}/{max(totals[name]):.3f}; mutex n={len(holds[name])} '
                  f'median/max ms {statistics.median(holds[name]):.3f}/{max(holds[name]):.3f}; '
                  f'post-body commit/release median/max ms {statistics.median(commits[name]):.3f}/{max(commits[name]):.3f}; '
                  f'max 128-VM blocks={max(blocks[name])}, max changes={max(writes[name])}')
        assert len(totals['save']) == 20 and all(len(totals[name]) >= 20 for name in ('upsert', 'retire', 'admit'))
        assert sum(count != 0 for count in writes['save']) == 20
        assert max(writes['save']) <= 3 and max(blocks['save']) < 16
        assert len(holds['save']) == 20 and all(max(blocks[name]) < 16 for name in ('upsert', 'admit'))
        assert max(blocks['retire']) < 32
        result = db.routing.load_routing_entries(routing_scope=store._routing_scope())
        assert len(result) == 10_000 and json.loads(result['agent:main:peer:2'])['last_prompt_tokens'] == 20
    finally:
        store.close_all_db_handles()
