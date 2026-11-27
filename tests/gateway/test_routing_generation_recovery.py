"""A gateway saves its own edits, not everything its last load didn't see."""

import json
from pathlib import Path
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore
from persistence.core.errors import StoreContention
from tests.fixtures.storage_holder import raw_holder


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    opened = []

    def start(mirror=False):
        store = SessionStore(tmp_path / 'sessions', GatewayConfig(write_sessions_json=mirror))
        opened.append(store)
        return store

    yield start
    for store in opened:
        store.close_all_db_handles()


def route(store, chat='one'):
    return store.get_or_create_session(SessionSource(Platform.TELEGRAM, chat_id=chat, chat_type='dm'))


def rows(store):
    return {key: json.loads(text)['session_id'] for key, text in
            store.persistence.routing.load_routing_entries(routing_scope=store._routing_scope()).items()}


def test_disabled_import_preserves_concurrent_upsert(stores):
    store = stores()
    store.sessions_dir.mkdir(exist_ok=True, parents=True)
    store._save_sessions_json({})
    routing = store.persistence.routing
    load = routing.load_routing_entries

    def race(**kwargs):
        result = load(**kwargs)
        routing.upsert_routing_entry('external', '{"session_id":"new"}', routing_scope=kwargs['routing_scope'])
        return result

    with patch.object(routing, 'load_routing_entries', race):
        store._ensure_loaded()
    assert rows(store) == {'external': 'new'}


def test_multiple_generations_do_not_undo_direct_edits_or_deletes(stores):
    store = stores()
    owned, repointed, deleted = (route(store, chat) for chat in ('own', 'moved', 'gone'))
    routing, scope = store.persistence.routing, store._routing_scope()
    routing.upsert_routing_entry(repointed.session_key, json.dumps({**repointed.to_dict(), 'session_id': 'direct'}),
                                 routing_scope=scope)
    raw_holder(store.persistence)._write_sql('DELETE FROM gateway_routing WHERE scope=? AND session_key=?',
                                          (scope, deleted.session_key))
    for version in ['first', 'second']:
        owned.session_id = version
        store._save_entries()
        assert rows(store) == {owned.session_key: version, repointed.session_key: 'direct'}
    assert {key: value.session_id for key, value in cold_entries(stores()).items()} == rows(store)


def cold_entries(store):
    store._ensure_loaded()
    return store._entries


def test_recovery_ignores_outer_transcript_snapshot(stores):
    store = stores()
    routing, holder = store.persistence.routing, raw_holder(store.persistence)
    with patch.object(routing, 'load_routing_entries', side_effect=OSError('temporarily unavailable')):
        store._ensure_loaded()
    with holder.transcript_read_snapshot():
        assert rows(store) == {}
        peer = stores()
        entry = route(peer, 'concurrent')
        assert entry.session_key in cold_entries(store)
        store._save_entries()
    assert rows(store)[entry.session_key] == entry.session_id


def test_fallback_replays_retarget_and_delete_without_clobbering_direct_upsert(stores):
    store = stores(True)
    changed, removed, other = (route(store, chat) for chat in ('changed', 'deleted', 'other'))
    holder = raw_holder(store.persistence)
    with patch.object(holder, '_execute_write', side_effect=OSError('temporary db failure')):
        changed.session_id = 'fallback'
        other.session_id = 'older-pending'
        store._entries.pop(removed.session_key)
        store._save_entries()
        with closing(sqlite3.connect(holder.db_path)) as peer, peer:
            peer.execute('UPDATE gateway_routing SET entry_json=? WHERE scope=? AND session_key=?',
                         (json.dumps({**other.to_dict(), 'session_id': 'direct'}), store._routing_scope(), other.session_key))
        changed.session_id = 'fallback-two'
        store._save_entries()
    cold = stores(False)
    with patch.object(Path, 'replace', side_effect=PermissionError('rename denied')):
        cold._ensure_loaded()
    expected = {changed.session_key: 'fallback-two', other.session_key: 'direct'}
    assert rows(cold) == expected
    cold.persistence.routing.upsert_routing_entry(changed.session_key, json.dumps({**changed.to_dict(), 'session_id': 'later'}),
                                                 routing_scope=cold._routing_scope())
    assert {key: value.session_id for key, value in cold_entries(stores()).items()} == {
        **expected, changed.session_key: 'later'}


def test_failed_archive_and_failed_mirror_cannot_resurrect_committed_delete(stores):
    first = stores(True)
    entry = route(first)
    mirror = first.sessions_dir / 'sessions.json'
    original = mirror.read_bytes()
    cold = stores(False)
    with (patch.object(Path, 'replace', side_effect=PermissionError('rename denied')),
          patch.object(cold, '_save_sessions_json', side_effect=OSError('mirror unavailable')) as write):
        cold._ensure_loaded()
        del cold._entries[entry.session_key]
        cold._save_entries()
        assert rows(cold) == {}
        assert mirror.read_bytes() == original
        assert entry.session_key not in cold_entries(stores())
        write.assert_not_called()


def test_disabled_failed_commit_rolls_back_and_does_not_publish_generation(stores):
    store = stores()
    changed, dropped = route(store), route(store, 'drop')
    before, generation = rows(store), store._persisted_routing_generation
    changed.session_id = 'must-rollback'
    del store._entries[dropped.session_key]
    holder = raw_holder(store.persistence)
    holder._write_sql("CREATE TRIGGER refuse BEFORE INSERT ON gateway_routing BEGIN SELECT RAISE(ABORT,'refused'); END")
    with pytest.raises(Exception, match='refused'):
        store._save_entries()
    assert rows(store) == before and store._persisted_routing_generation == generation
    assert not (store.sessions_dir / 'sessions.json').exists()
    holder._write_sql('DROP TRIGGER refuse')
    store._save_entries()
    assert rows(store) == {changed.session_key: 'must-rollback'}


def test_delayed_incomplete_snapshot_publishes_its_edit_not_a_scope_replace(stores):
    first = stores(True)
    existing = route(first)
    store = stores(False)
    routing = store.persistence.routing
    with patch.object(routing, 'load_routing_entries', side_effect=OSError('reader offline')):
        store._ensure_loaded()
        with store._lock:
            store._entries[existing.session_key].session_id = 'queued'
            snapshot = store._snapshot_routing_locked()
    external = route(first, 'outside')
    store._ensure_loaded()
    assert rows(store)[existing.session_key] == existing.session_id
    store._persist_routing_data(*snapshot)
    assert rows(store) == {existing.session_key: 'queued', external.session_key: external.session_id}


def test_unchanged_new_fence_filters_mirror_without_deleting_direct_repoint(stores):
    store = stores(True)
    entry = route(store)
    holder, write = raw_holder(store.persistence), raw_holder(store.persistence)._execute_write
    attempts = []

    def race(callback):
        if not attempts:
            with closing(sqlite3.connect(holder.db_path)) as peer, peer:
                peer.execute('INSERT INTO state_meta VALUES (?,?)', ('gateway.retired_route.v1.main:' + entry.session_id, '{}'))
                peer.execute('DELETE FROM sessions WHERE id=?', (entry.session_id,))
                peer.execute('UPDATE gateway_routing SET entry_json=? WHERE scope=? AND session_key=?',
                             (json.dumps({**entry.to_dict(), 'session_id': 'direct'}), store._routing_scope(), entry.session_key))
        attempts.append(True)
        return write(callback)

    with patch.object(holder, '_execute_write', race):
        store._save_entries()
    assert entry.session_key not in json.loads((store.sessions_dir / 'sessions.json').read_text())
    assert rows(store) == {entry.session_key: 'direct'}
    assert cold_entries(stores())[entry.session_key].session_id == 'direct'


def test_failed_commit_after_replay_uses_reconciled_before_image(stores):
    store = stores(True)
    entry = route(store)
    routing = store.persistence.routing
    with patch.object(routing, 'save_routing_edits', side_effect=OSError('commit unavailable')):
        entry.session_id = 'first-journal'
        store._save_entries()
        entry.session_id = 'second-journal'
        store._save_entries()
        assert rows(store)[entry.session_key] == 'first-journal'
    assert cold_entries(stores())[entry.session_key].session_id == 'second-journal'


def test_legacy_import_checks_same_key_upsert_and_retirement_inside_transaction(stores):
    seed = stores(True)
    kept, gone = route(seed, 'upsert'), route(seed, 'retired')
    mirror = {entry.session_key: entry.to_dict() for entry in (kept, gone)}
    seed._save_sessions_json(mirror)
    seed.persistence.routing.replace_routing_entries({}, routing_scope=seed._routing_scope())
    cold = stores(False)
    routing = cold.persistence.routing
    reconcile = routing.reconcile_routing_mirror

    def racing(*args, **kwargs):
        routing.upsert_routing_entry(kept.session_key, json.dumps({**kept.to_dict(), 'session_id': 'direct'}),
                                     routing_scope=cold._routing_scope())
        raw_holder(cold.persistence)._write_sql('INSERT INTO state_meta VALUES (?,?)',
                                              ('gateway.retired_session.v1.' + gone.session_id, '{}'))
        return reconcile(*args, **kwargs)

    with patch.object(routing, 'reconcile_routing_mirror', racing):
        assert {key: entry.session_id for key, entry in cold_entries(cold).items()} == {kept.session_key: 'direct'}
    assert rows(cold) == {kept.session_key: 'direct'}


def test_retirement_after_sql_commit_before_mirror_publish_cannot_replay(stores):
    store = stores(True)
    entry = route(store)
    routing, publish = store.persistence.routing, store._save_sessions_json

    def race(data):
        routing.fence_retired_routes([entry.session_id], namespace='main', expiring=[entry.session_id])
        raw_holder(store.persistence)._write_sql('DELETE FROM state_meta WHERE key=?',
                                                ('gateway.retired_route.v1.main:' + entry.session_id,))
        publish(data)

    with patch.object(store, '_save_sessions_json', race):
        store._save_entries()
    assert entry.session_key in json.loads((store.sessions_dir / 'sessions.json').read_text())
    assert rows(store) == {} and cold_entries(stores()) == {}


def test_retarget_to_fenced_id_drops_prior_binding_but_not_newer_direct_binding(stores):
    store = stores(True)
    old, repointed = route(store, 'old'), route(store, 'repointed')
    store.persistence.routing.upsert_routing_entry(repointed.session_key, json.dumps({**repointed.to_dict(), 'session_id': 'direct'}),
                                                 routing_scope=store._routing_scope())
    store.persistence.routing.fence_retired_routes(['dead'], namespace='main')
    old.session_id = repointed.session_id = 'dead'
    store._save_entries()
    assert rows(store) == {repointed.session_key: 'direct'}
    assert set(cold_entries(stores())) == {repointed.session_key}


def test_transient_reader_contention_never_acknowledges_a_json_only_save(stores):
    store = stores(True)
    entry = route(store)
    generation = store._persisted_routing_generation
    entry.session_id = 'changed'
    with (patch.object(store.persistence.routing, 'save_routing_edits', side_effect=StoreContention('busy')),
          patch.object(store, '_save_sessions_json') as write, pytest.raises(StoreContention)):
        store._save_entries()
    write.assert_not_called()
    assert store._persisted_routing_generation == generation and rows(store)[entry.session_key] != 'changed'
    store._save_entries()
    assert rows(store)[entry.session_key] == 'changed'
