"""A page of a compression lineage's display transcript does not re-read the whole lineage.

Every Bot Chat is a lineage of compression segments (6 to 25 on a live install, 2k to 15k rows, 5 to
40 MB of bodies), and ``GET /api/sessions/<id>/messages?limit=120`` read and deduped all of it for each
page: 0.6 to 2.8 s of CPU per chat switch. ``lineage_page`` keeps the deduped order under a
stamp of the lineage's rows; these tests pin that a page of an unchanged lineage reuses it (across
handles, as the REST routes open one per request) and that every kind of transcript change is seen by
the very next read. Real store holder, real compression rotation.
"""
import pytest

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.conversation._store import lineage_page
from persistence.sqlite.conversation._store import messages as store_messages
from persistence.sqlite.conversation._store.compression import publish_compression_child
from persistence.sqlite.conversation._store.messages import (
    _resume_lineage_ids,
    append_messages_batch,
    get_messages,
    rewind_to_message,
)
from persistence.sqlite.conversation._store.sessions import create_session
from tests.persistence_sqlite.core._lent_store import lent_store

PAGES = [(4, 0, True), (4, 4, True), (3, 0, False), (5, 2, False), (500, 0, True), (2, 1000, True)]


def _turns(segment, count=3):
    return [message for n in range(count) for message in (
        {'role': 'user', 'content': f'ask {segment}.{n}'}, {'role': 'assistant', 'content': f'reply {segment}.{n}'})]


def _rotate(db, parent, child):
    """Compress *parent* into *child*: a summary plus the carried tail, as the agent publishes it."""
    tail = get_messages(db, parent)[-2:]
    coordination = lent_store(db).coordination
    assert coordination.try_acquire_compression_lock(session_id=parent, holder='fixture')
    try:
        publish_compression_child(
            db, parent_session_id=parent, child_session_id=child, source='test', compression_lock_holder='fixture',
            messages=[{'role': 'user', 'content': f'[CONTEXT SUMMARY] of {parent}'},
                      *({'role': m['role'], 'content': m['content']} for m in tail)])
    finally:
        coordination.release_compression_lock(session_id=parent, holder='fixture')


def _lineage(db, segments=3):
    create_session(db, 's0', source='test')
    append_messages_batch(db, 's0', _turns(0))
    for n in range(1, segments):
        _rotate(db, f's{n - 1}', f's{n}')
        append_messages_batch(db, f's{n}', _turns(n))
    tip = f's{segments - 1}'
    assert len(_resume_lineage_ids(db, tip)) == segments
    return tip


def _view(messages):
    return [(m['id'], m['session_id'], m['role'], m['content']) for m in messages]


def _page(db, tip, limit, offset, latest):
    return _view(get_messages(db, tip, limit=limit, offset=offset, latest=latest,
                              include_compacted=True, include_ancestors=True))


def _full(db, tip):
    """The whole deduped lineage: the unpaged read, which never uses the kept projection."""
    return _view(get_messages(db, tip, include_compacted=True, include_ancestors=True))


def _sliced(rows, limit, offset, latest):
    return rows[::-1][offset:][:limit][::-1] if latest else rows[offset:][:limit]


def _assert_pages_match(db, tip):
    full = _full(db, tip)
    for limit, offset, latest in PAGES:
        assert _page(db, tip, limit, offset, latest) == _sliced(full, limit, offset, latest), (limit, offset, latest)
    return full


@pytest.fixture
def projections(monkeypatch):
    """How many times a whole lineage was deduped, on any handle."""
    sizes = []
    real = store_messages._dedupe_display_generations

    def spy(holder, rows, **kwargs):
        rows = rows if isinstance(rows, list) else list(rows)
        sizes.append(len(rows))
        return real(holder, rows, **kwargs)

    monkeypatch.setattr(store_messages, '_dedupe_display_generations', spy)
    return sizes


def test_pages_of_a_lineage_match_the_full_projection(tmp_path):
    with StoreHolder(tmp_path / 'state.db') as db:
        tip = _lineage(db)
        full = _assert_pages_match(db, tip)
        assert {row[1] for row in full} == {'s0', 's1', 's2'}  # the page really spans the segments
        _assert_pages_match(db, tip)  # and again, from the kept projection


def test_an_unchanged_lineage_is_projected_once_across_pages_and_handles(tmp_path, projections):
    path = tmp_path / 'state.db'
    with StoreHolder(path) as db:
        tip = _lineage(db)
        first = _page(db, tip, 4, 0, True)
        del projections[:]
        for _ in range(5):
            assert _page(db, tip, 4, 0, True) == first
        older = _page(db, tip, 4, 4, True)  # "load earlier" pages back through the same projection
        assert older and older != first
        # The REST transcript routes open a fresh read-only handle per request.
        for _ in range(3):
            with StoreHolder(path, read_only=True) as reader:
                assert _page(reader, tip, 4, 0, True) == first
        assert projections == []


def test_a_growing_lineage_extends_the_projection_without_a_full_dedupe(tmp_path, projections):
    """A turn appending rows: each page read dedupes the new rows alone, never the whole lineage again."""
    path = tmp_path / 'state.db'
    with StoreHolder(path) as db:
        tip = _lineage(db)
        shown = len(_page(db, tip, 500, 0, True))
        for n in range(4):
            append_messages_batch(db, tip, [{'role': 'user', 'content': f'more {n}'}, {'role': 'assistant', 'content': f'done {n}'}])
            del projections[:]
            with StoreHolder(path, read_only=True) as reader:
                page = _page(reader, tip, 4, 0, True)
            assert [row[3] for row in page[-2:]] == [f'more {n}', f'done {n}']
            assert projections == [2], projections
            _assert_pages_match(db, tip)
        assert len(_page(db, tip, 500, 0, True)) == shown + 8


@pytest.mark.parametrize('appended', ['copy_of_an_earlier_row', 'earlier_uid', 'model_only', 'twins_in_one_batch'])
def test_an_appended_row_that_is_not_simply_new_is_projected_like_the_full_read(tmp_path, appended):
    with StoreHolder(tmp_path / 'state.db') as db:
        tip = _lineage(db)
        before = _assert_pages_match(db, tip)
        earlier = next(m for m in get_messages(db, 's0') if m['content'] == 'reply 0.1')
        if appended == 'copy_of_an_earlier_row':
            # Same role, content and timestamp: one logical message, its newest copy now in the tip.
            append_messages_batch(db, tip, [{'role': earlier['role'], 'content': earlier['content'],
                                             'timestamp': earlier['timestamp']}])
        elif appended == 'earlier_uid':
            if not earlier.get('message_uid'):
                pytest.skip('this store mints no message uids')
            append_messages_batch(db, tip, [{'role': 'assistant', 'content': 'rewritten later',
                                             'message_uid': earlier['message_uid']}])
        elif appended == 'model_only':
            append_messages_batch(db, tip, [{'role': 'user', 'content': 'for the model only',
                                             'display_metadata': {'model_only': True}}])
        else:
            append_messages_batch(db, tip, [{'role': 'user', 'content': 'twin'}, {'role': 'user', 'content': 'twin'}])
        after = _assert_pages_match(db, tip)
        contents = [row[3] for row in after]
        if appended == 'copy_of_an_earlier_row':
            assert contents.count('reply 0.1') == 1 and len(after) == len(before)
        elif appended == 'model_only':
            assert 'for the model only' not in contents and len(after) == len(before)
        elif appended == 'twins_in_one_batch':
            assert contents.count('twin') >= 1
        # And the next plain append still lands on a correct projection.
        append_messages_batch(db, tip, [{'role': 'user', 'content': 'and then'}])
        assert _assert_pages_match(db, tip)[-1][3] == 'and then'


@pytest.mark.parametrize('change', ['append', 'rewind', 'rewrite', 'delete', 'slot', 'rotate'])
def test_every_transcript_change_is_seen_by_the_next_page(tmp_path, change):
    with StoreHolder(tmp_path / 'state.db') as db:
        tip = _lineage(db)
        before = _assert_pages_match(db, tip)
        last = _page(db, tip, 4, 0, True)
        if change == 'append':
            append_messages_batch(db, tip, [{'role': 'user', 'content': 'NEW ASK'}, {'role': 'assistant', 'content': 'NEW REPLY'}])
        elif change == 'rewind':
            rewind_to_message(db, tip, next(m['id'] for m in get_messages(db, tip) if m['content'] == 'ask 2.2'))
        elif change == 'rewrite':
            db._execute_write(lambda conn: conn.execute("UPDATE messages SET content = 'EDITED' WHERE id = ?", (last[-1][0],)))
        elif change == 'delete':
            db._execute_write(lambda conn: conn.execute("DELETE FROM messages WHERE id = ?", (last[-1][0],)))
        elif change == 'slot':
            db._execute_write(lambda conn: conn.execute("UPDATE messages SET display_order = NULL WHERE id = ?", (last[-1][0],)))
        else:
            _rotate(db, tip, 's3')
            append_messages_batch(db, 's3', _turns(3))
            tip = 's3'
        after = _assert_pages_match(db, tip)
        page = _page(db, tip, 4, 0, True)
        if change == 'append':
            assert [row[3] for row in page[-2:]] == ['NEW ASK', 'NEW REPLY']
        elif change == 'rewind':
            assert 'ask 2.2' not in [row[3] for row in after] and len(after) < len(before)
        elif change == 'rewrite':
            assert page[-1][3] == 'EDITED'
        elif change == 'delete':
            assert last[-1][0] not in [row[0] for row in after]
        elif change == 'rotate':
            assert page[-1][3] == 'reply 3.2' and {row[1] for row in after} >= {'s0', 's3'}


def test_stores_and_recreated_files_never_share_a_projection(tmp_path):
    pages = {}
    for name in ('one', 'two'):
        with StoreHolder(tmp_path / f'{name}.db') as db:
            tip = _lineage(db)
            append_messages_batch(db, tip, [{'role': 'user', 'content': f'only in {name}'}])
            pages[name] = _page(db, tip, 4, 0, True)
            _assert_pages_match(db, tip)
    assert pages['one'][-1][3] == 'only in one' and pages['two'][-1][3] == 'only in two'
    # The same path, another file: a recovered or recreated store.
    (tmp_path / 'one.db').unlink()
    for suffix in ('-wal', '-shm'):
        (tmp_path / f'one.db{suffix}').unlink(missing_ok=True)
    with StoreHolder(tmp_path / 'one.db') as db:
        tip = _lineage(db, segments=2)
        _assert_pages_match(db, tip)
        assert 'only in one' not in [row[3] for row in _full(db, tip)]


def test_a_store_without_the_stamp_columns_reads_the_whole_lineage(tmp_path, projections, monkeypatch):
    """A legacy read-only store cannot be stamped: the page is read as before, never guessed."""
    with StoreHolder(tmp_path / 'state.db') as db:
        tip = _lineage(db)
        monkeypatch.setattr(lineage_page, '_STAMP_COLUMNS', frozenset({'active', 'a_column_this_store_lacks'}))
        expected = _sliced(_full(db, tip), 4, 0, True)
        del projections[:]
        assert _page(db, tip, 4, 0, True) == expected
        assert _page(db, tip, 4, 0, True) == expected
        assert len(projections) == 2
