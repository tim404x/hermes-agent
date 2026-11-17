"""A session listing re-parses only the receipts that moved, never every receipt of the store.

``local_lineage_index`` JSON-parses every ``gateway.local_policy.v1:*`` row to find the few whose
lineage has more than one segment. Receipts are about 36 KB each and every cron run leaves one: a live
profile with 8,900 of them parsed 317 MB of JSON (0.4 s of CPU) per listing, on every commit to the store
and on every fresh handle (``profiles.list`` every 5 s, the REST routes). These tests pin that an
unrelated commit or a fresh handle costs no second full scan, and that every way a receipt can move is
seen by the very next listing, with the same answer the full scan gives.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hermes_state import SessionDB
from hermes_state_local import POLICY_PREFIX, _cached_local_lineage_index, local_lineage_index


def _receipt(owner, lineage, **extra):
    return json.dumps({'session_id': owner, 'lineage': lineage, **extra})


def _put(conn, owner, value):
    conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (POLICY_PREFIX + owner, value))


@pytest.fixture
def db(tmp_path):
    database = SessionDB(db_path=tmp_path / 'state.db')
    for sid in ('owner', 'head', 'tail', 'solo', 'comp', 'cont'):
        database.create_session(sid, source='cli')
    conn = database._conn
    assert conn is not None
    conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
    conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='comp'")
    _put(conn, 'owner', _receipt('owner', ['owner', 'head']))
    _put(conn, 'comp', _receipt('comp', ['comp', 'cont']))             # a compression step: one segment still
    _put(conn, 'solo', json.dumps({'session_id': 'solo', 'policy': 'x' * 2000}))  # no lineage: a cron run's
    _put(conn, 'single', _receipt('single', ['single']))
    _put(conn, 'broken', '{not json')
    _put(conn, 'odd-owner', json.dumps({'session_id': 7, 'lineage': ['a', 'b']}))
    _put(conn, 'odd-lineage', json.dumps({'session_id': 'odd-lineage', 'lineage': ['a', 3]}))
    _put(conn, 'array', json.dumps(['session_id', 'lineage']))
    conn.commit()
    yield database
    database.close()


def _full_scans(statements):
    """The scan that parses every receipt's body (on any path, old or new)."""
    return [sql for sql in statements if 'FROM state_meta' in sql and 'GLOB' in sql and 'json_array_length' in sql]


def _both(database):
    """``(what a listing uses, what the full scan answers)`` on one committed state."""
    with database._read_ctx() as conn:
        return _cached_local_lineage_index(database, conn), local_lineage_index(conn)


def test_the_listing_index_equals_the_full_scan(db):
    used, scanned = _both(db)
    assert used == scanned
    assert used == ({'owner'}, {'head': ('owner', ['owner', 'head']), 'comp': ('comp', ['comp', 'cont'])})
    assert _both(db)[0] == scanned  # and again, from what was kept


def test_an_unrelated_commit_does_not_parse_every_receipt_again(db):
    statements = []
    with db._read_ctx() as conn:
        conn.set_trace_callback(statements.append)
    assert [row['id'] for row in db.list_sessions_rich(limit=50) if row['id'] in ('owner', 'head')] == ['head']
    assert len(_full_scans(statements)) == 1
    for n in range(5):
        # Any commit moves the reader's data_version: a new chat, a turn's rows, a cron run.
        db.create_session(f'unrelated-{n}', source='cli')
        db.append_messages_batch(f'unrelated-{n}', [{'role': 'user', 'content': 'hi'}])
        rows = db.list_sessions_rich(limit=50)
        assert 'owner' not in [row['id'] for row in rows]
        assert set(next(row for row in rows if row['id'] == 'head')['_lineage_ids']) == {'owner', 'head'}
    assert len(_full_scans(statements)) == 1


def test_a_fresh_handle_does_not_parse_every_receipt_again(db):
    assert 'owner' not in [row['id'] for row in db.list_sessions_rich(limit=50)]
    for _ in range(3):
        # The roster's profiles.list and the REST session routes open a handle per request.
        with SessionDB(db_path=db.db_path, read_only=True) as reader:
            statements = []
            with reader._read_ctx() as conn:
                conn.set_trace_callback(statements.append)
            assert 'owner' not in [row['id'] for row in reader.list_sessions_rich(limit=50)]
            assert _full_scans(statements) == []


@pytest.mark.parametrize('change', [
    'extend', 'same_length_rewrite', 'retire', 'replace', 'new_receipt', 'end_reason', 'becomes_invalid', 'bulk'])
def test_every_receipt_change_is_seen_by_the_next_listing(db, change):
    assert _both(db)[0] == _both(db)[1]
    conn = db._conn
    assert conn is not None
    key = POLICY_PREFIX + 'owner'
    if change == 'extend':
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='head'")
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_receipt('owner', ['owner', 'head', 'tail']), key))
    elif change == 'same_length_rewrite':
        # A lineage write that keeps the receipt's byte length: its owner's revision moves with it
        # (hermes_state_local_lineage._commit_local_target bumps it in the same transaction).
        before, after = _receipt('owner', ['owner', 'head']), _receipt('owner', ['owner', 'tail'])
        assert len(before) == len(after)
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (after, key))
        conn.execute("UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id='owner'")
    elif change == 'retire':
        conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
    elif change == 'replace':
        conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)', (key, _receipt('owner', ['owner', 'tail'])))
    elif change == 'new_receipt':
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='solo'")
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (
            _receipt('solo', ['solo', 'tail'], policy='x' * 2000), POLICY_PREFIX + 'solo'))
    elif change == 'end_reason':
        conn.execute("UPDATE sessions SET end_reason='compression' WHERE id='owner'")
    elif change == 'becomes_invalid':
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', ('{broken now', key))
    else:
        # More receipts move at once than are re-read one by one.
        for n in range(150):
            db.create_session(f'bulk-{n}', source='cli')
            db.create_session(f'bulk-{n}-next', source='cli')
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id LIKE 'bulk-%' AND id NOT LIKE '%-next'")
        for n in range(150):
            _put(conn, f'bulk-{n}', _receipt(f'bulk-{n}', [f'bulk-{n}', f'bulk-{n}-next']))
    conn.commit()
    used, scanned = _both(db)
    assert used == scanned
    superseded, heads = used
    expected = {
        'extend': ({'owner', 'head'}, {'tail': ('owner', ['owner', 'head', 'tail'])}),
        'same_length_rewrite': ({'owner'}, {'tail': ('owner', ['owner', 'tail'])}),
        'retire': (set(), {}),
        'replace': ({'owner'}, {'tail': ('owner', ['owner', 'tail'])}),
        'new_receipt': ({'owner', 'solo'}, {'head': ('owner', ['owner', 'head']), 'tail': ('solo', ['solo', 'tail'])}),
        'end_reason': (set(), {'owner': ('owner', ['owner', 'head'])}),
        'becomes_invalid': (set(), {}),
    }.get(change)
    if expected is not None:
        assert superseded == expected[0]
        assert {head: found for head, found in heads.items() if head != 'comp'} == expected[1]
    else:
        assert len(superseded) == 151 and heads['bulk-149-next'] == ('bulk-149', ['bulk-149', 'bulk-149-next'])


def test_an_open_transaction_never_reads_or_feeds_what_was_kept(db):
    conn = db._conn
    assert conn is not None
    committed = _cached_local_lineage_index(db, conn)
    conn.execute('BEGIN')
    conn.execute('DELETE FROM state_meta WHERE key=?', (POLICY_PREFIX + 'owner',))
    assert _cached_local_lineage_index(db, conn)[0] == set()  # the transaction's own view
    conn.rollback()
    assert _cached_local_lineage_index(db, conn) == committed
    with db._read_ctx() as reader:
        assert _cached_local_lineage_index(db, reader) == committed


def test_concurrent_first_listings_agree(db):
    barrier = Barrier(6)

    def read(_):
        barrier.wait(timeout=10)
        with SessionDB(db_path=db.db_path, read_only=True) as reader:
            with reader._read_ctx() as conn:
                return _cached_local_lineage_index(reader, conn)

    with db._read_ctx() as conn:
        expected = local_lineage_index(conn)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(read, range(6)):
            assert result == expected


def test_stores_never_share_receipts(db, tmp_path):
    assert _both(db)[0][0] == {'owner'}
    with SessionDB(db_path=tmp_path / 'other.db') as other:
        other.create_session('owner', source='cli')
        other.create_session('else', source='cli')
        conn = other._conn
        assert conn is not None
        conn.execute("UPDATE sessions SET end_reason='session_reset' WHERE id='owner'")
        _put(conn, 'owner', _receipt('owner', ['owner', 'else']))
        conn.commit()
        used, scanned = _both(other)
        assert used == scanned == ({'owner'}, {'else': ('owner', ['owner', 'else'])})
    assert _both(db)[0][1]['head'] == ('owner', ['owner', 'head'])
