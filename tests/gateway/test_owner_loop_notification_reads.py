"""Completion / watch-notice delivery never reads a route's whole history on the owner loop (t_7e4c9e3c).

24 Nov 2026, 1:43-1:56 PM PT: the profile gateway's event loop stalled 15, 43 and 73 s (``ws write slow (loop
stalled >10.0s)``), dropping every worker and cron socket. py-spy of the loop thread put 100% of the stall
samples in three synchronous reads on the notification path, each sized by the chat's history rather than by
the answer: ``completion_admission`` and ``committed_route_envelope`` decoded every admission of the route
(``admissions.list(pending_only=False)``: 2,007 rows on Tim's main chat) to find one identity / the newest
envelope, and ``SessionAuthority.logical_owner`` walked the whole compression lineage (``lineage.lineage``) to
read its root, several times per notification. Under a busy owner each row and query re-takes the GIL, so each
read took seconds.

Pinned here on a real owner: those reads go through ``admissions.mentioning``, ``admissions.latest_admission``
(newest first, stopping at the answer) and ``lineage.lineage_root``; the full-history verbs are never called on
that path, and the answers are unchanged.
"""
import asyncio

import pytest

from tests.gateway.test_automation_compression_media import _admit_human, _discord_owner


def _forbid_full_history_reads(monkeypatch):
    from persistence.sqlite.authority.admissions import SQLiteAdmissionRepository
    from persistence.sqlite.conversation.lineage import SQLiteLineageRepository

    def full_list(self, *, session_id, pending_only=True):
        if not pending_only:
            raise AssertionError(f'full admission history of {session_id} read on the notification path')
        return real_list(self, session_id=session_id, pending_only=pending_only)

    def full_lineage(self, session_id):
        raise AssertionError(f'whole compression lineage of {session_id} walked for its root')

    real_list = SQLiteAdmissionRepository.list
    monkeypatch.setattr(SQLiteAdmissionRepository, 'list', full_list)
    monkeypatch.setattr(SQLiteLineageRepository, 'lineage', full_lineage)


def _count_decoded_rows(monkeypatch):
    import persistence.sqlite.authority._admission_store as admission_store
    decoded = []
    real_row = admission_store._row

    def counting(row):
        decoded.append(row['admission_id'] if row is not None else None)
        return real_row(row)
    monkeypatch.setattr(admission_store, '_row', counting)
    return decoded


async def _owner_with_history(tmp_path, monkeypatch, *, humans=30):
    """A Discord route with a compressed transcript (root -> child), ``humans`` admitted messages and one
    admitted completion."""
    from gateway.platforms.event import MessageEvent
    from persistence.conversation.lineage import CompressionChild
    from persistence.conversation.messages import NewMessage
    from tests.fixtures.storage_seed import seed_messages
    from tests.gateway.test_completion_admission import pending

    runner, _, authority, source = await _discord_owner(tmp_path, monkeypatch)
    root = None
    for index in range(humans):
        receipt = await _admit_human(runner, authority, MessageEvent(text=f'human {index}', source=source,
                                                                     message_id=f'human-{index}'))
        root = root or receipt.ref.session_id
        claimed = authority.persistence.admissions.claim(epoch=authority.epoch, session_id=root)
        authority.persistence.admissions.settle(epoch=authority.epoch, admission_id=claimed.admission_id,
                                                generation=claimed.generation, outcome='completed')
    key = runner.session_store._generate_session_key(source)
    db = authority.persistence
    seed_messages(db, root, {'role': 'user', 'content': 'human'})
    child = root + '-child'
    db.lineage.publish_compression_child(root, CompressionChild(session_id=child, source='discord'),
                                         [NewMessage.from_live({'role': 'user', 'content': 'summary'})],
                                         require_compression_lease=False)
    assert runner.session_store.advance_compression_session(key, root, child) is not None
    first = dict(pending(key, 'first-completion'), parent_session_id=child)
    assert await runner._deliver_async_delegation_group([first]) is True
    return runner, authority, key, root, child, first


@pytest.mark.asyncio
async def test_completion_identity_probe_decodes_only_the_rows_that_carry_it(tmp_path, monkeypatch):
    from gateway.session_automation import completion_admission
    from tests.gateway.test_completion_admission import pending

    runner, authority, key, root, _child, first = await _owner_with_history(tmp_path, monkeypatch)
    rows = authority.persistence.admissions.list(session_id=root, pending_only=False)
    assert len(rows) == 31 and 'first-completion' in rows[-1].payload['text']
    _forbid_full_history_reads(monkeypatch)
    decoded = _count_decoded_rows(monkeypatch)
    # A lost-ACK redelivery is still found (same admission), from the one row that carries its identity.
    assert completion_admission(runner, first)['admission_id'] == rows[-1].admission_id
    assert decoded == [rows[-1].admission_id]
    decoded.clear()
    # A new completion is not admitted yet: nothing in the 31-row history is decoded to say so.
    fresh = dict(pending(key, 'second-completion'), parent_session_id=first['parent_session_id'])
    assert completion_admission(runner, fresh) is None
    assert decoded == []
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_completion_is_delivered_without_reading_the_route_history(tmp_path, monkeypatch):
    from tests.gateway.test_completion_admission import pending

    runner, authority, key, root, child, _first = await _owner_with_history(tmp_path, monkeypatch)
    _forbid_full_history_reads(monkeypatch)
    second = dict(pending(key, 'second-completion'), parent_session_id=child)
    assert await runner._deliver_async_delegation_group([second]) is True
    monkeypatch.undo()
    rows = authority.persistence.admissions.list(session_id=root, pending_only=False)
    assert len(rows) == 32 and 'second-completion' in rows[-1].payload['text']
    assert authority.persistence.admissions.list(session_id=child, pending_only=False) == []
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_borrowed_envelope_is_read_newest_first_and_unchanged(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gateway.session_automation import committed_route_envelope

    _runner, authority, _key, root, child, _first = await _owner_with_history(tmp_path, monkeypatch, humans=12)
    rows = authority.persistence.admissions.list(session_id=root, pending_only=False)
    newest_human = rows[-2].payload['native_text_v1']
    assert 'provenance' in newest_human and 'automation' not in newest_human
    _forbid_full_history_reads(monkeypatch)
    decoded = _count_decoded_rows(monkeypatch)
    entry = SimpleNamespace(session_id=child)  # the route's transcript tip
    # The automation's own envelope (newest) is skipped for a human-only borrow; the newest human one wins.
    assert committed_route_envelope(authority, entry, human_only=True) == newest_human
    assert decoded == [rows[-1].admission_id, rows[-2].admission_id]
    decoded.clear()
    assert committed_route_envelope(authority, entry, human_only=True, user_id='someone-else') is None
    assert len(decoded) == len(rows)  # no match: the stream ends where the history does, as before
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_logical_owner_reads_the_root_without_the_whole_lineage(tmp_path, monkeypatch):
    _runner, authority, _key, root, child, _first = await _owner_with_history(tmp_path, monkeypatch, humans=1)
    _forbid_full_history_reads(monkeypatch)
    assert authority.logical_owner(child) == root
    assert authority.logical_owner(root) == root
    assert authority.logical_owner('never-recorded') == 'never-recorded'
    assert authority.logical_owner('') == ''
    await asyncio.sleep(0)
