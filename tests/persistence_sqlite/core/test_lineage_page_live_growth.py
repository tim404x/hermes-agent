"""Live-turn growth extension (digest-verified tip rewrites) and single-flight bounded rebuilds.

During a live turn, ``sessions.transcript_revision`` moves on writes that do not alter the display
projection (``api_content`` backfills on the user row, ``codex_reasoning_items`` checkpoint pruning
on older assistant rows, non-visibility ``display_metadata`` stamps on assistant rows). Without
tip-digest verification, every poll during a live turn fell back to a full-lineage rebuild, and
without single-flight + a process-wide rebuild bound, concurrent polls for the same lineage ran
parallel full-lineage dedupes fighting for the GIL.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.conversation._store import lineage_page
from persistence.sqlite.conversation._store import messages as store_messages
from persistence.sqlite.conversation._store.api_content import set_message_api_content
from persistence.sqlite.conversation._store.messages import (
    _resume_lineage_ids,
    append_messages_batch,
    get_messages,
    rewind_to_message,
    set_user_message_content,
)
from persistence.sqlite.conversation._store.sessions import create_session
from tests.persistence_sqlite.core.test_lineage_display_page import (
    PAGES,
    _full,
    _lineage,
    _page,
    _rotate,
    _sliced,
    _turns,
    _view,
)


def _fresh_page(path, tip, limit=4, offset=0, latest=True):
    """Paged display read over a fresh read-only handle, matching the REST transcript routes."""
    with StoreHolder(path, read_only=True) as reader:
        return _page(reader, tip, limit, offset, latest)


def _fresh_raw_page(path, tip, limit=4, offset=0, latest=True):
    with StoreHolder(path, read_only=True) as reader:
        return get_messages(
            reader, tip, limit=limit, offset=offset, latest=latest,
            include_compacted=True, include_ancestors=True,
        )


def _named_lineage(db, prefix: str, segments: int = 3) -> str:
    create_session(db, f"{prefix}0", source="test")
    append_messages_batch(db, f"{prefix}0", _turns(f"{prefix}0"))
    for n in range(1, segments):
        _rotate(db, f"{prefix}{n - 1}", f"{prefix}{n}")
        append_messages_batch(db, f"{prefix}{n}", _turns(f"{prefix}{n}"))
    tip = f"{prefix}{segments - 1}"
    assert len(_resume_lineage_ids(db, tip)) == segments
    return tip


class _FullReadCounter:
    """Count full-lineage reads initiated by paged reads (excluding unpaged ground-truth oracle reads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._local = threading.local()
        self.count = 0

    @contextmanager
    def oracle(self):
        prev = getattr(self._local, "in_oracle", False)
        self._local.in_oracle = True
        try:
            yield
        finally:
            self._local.in_oracle = prev

    def note(self) -> None:
        if getattr(self._local, "in_oracle", False):
            return
        with self._lock:
            self.count += 1

    def reset(self) -> None:
        with self._lock:
            self.count = 0


@pytest.fixture
def full_reads(monkeypatch) -> _FullReadCounter:
    counter = _FullReadCounter()
    real = store_messages._with_removed_identities

    def spy(holder, rows, session_ids, conn=None):
        counter.note()
        return real(holder, rows, session_ids, conn)

    monkeypatch.setattr(store_messages, "_with_removed_identities", spy)
    return counter


def _assert_pages_match(db, tip, full_reads: _FullReadCounter):
    with full_reads.oracle():
        full = _full(db, tip)
    for limit, offset, latest in PAGES:
        assert _page(db, tip, limit, offset, latest) == _sliced(full, limit, offset, latest), (limit, offset, latest)
    return full


def _set_display_metadata(db: StoreHolder, row_id: int, meta: dict) -> None:
    payload = json.dumps(meta)
    db._execute_write(
        lambda conn: conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ?", (payload, row_id))
    )


def _set_codex_reasoning(db: StoreHolder, row_id: int, items: list[dict] | None) -> None:
    payload = json.dumps(items) if items is not None else None
    db._execute_write(
        lambda conn: conn.execute("UPDATE messages SET codex_reasoning_items = ? WHERE id = ?", (payload, row_id))
    )


# ── 1. Equivalence + extend-vs-rebuild per write pattern ────────────────────


def test_plain_append_extends(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        full_reads.reset()

        append_messages_batch(db, tip, [
            {"role": "user", "content": "plain ask"},
            {"role": "assistant", "content": "plain reply"},
        ])
        page = _fresh_page(path, tip)
        assert [row[3] for row in page[-2:]] == ["plain ask", "plain reply"]
        assert full_reads.count == 0
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0


def test_api_content_backfill_on_old_user_row_extends(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        full_reads.reset()

        append_messages_batch(db, tip, [{"role": "user", "content": "live ask"}])
        page = _fresh_page(path, tip)
        user_row_id = page[-1][0]
        assert full_reads.count == 0

        updated = set_message_api_content(db, tip, user_row_id, "live ask", "<system-context>live ask")
        assert updated == 1

        page_after = _fresh_page(path, tip)
        assert page_after == page
        assert full_reads.count == 0
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0


def test_non_model_only_display_metadata_on_assistant_extends_both_before_and_after_read(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        full_reads.reset()

        # Case 1: append assistant row, read page, THEN write non-model_only display_metadata on that old row.
        append_messages_batch(db, tip, [{"role": "assistant", "content": "delivered reply"}])
        seen = _fresh_raw_page(path, tip)
        assistant_id = seen[-1]["id"]
        assert full_reads.count == 0

        _set_display_metadata(db, assistant_id, {"delivered": True, "delivery_ms": 42})
        after = _fresh_raw_page(path, tip)
        assert after[-1]["id"] == assistant_id
        assert after[-1]["display_metadata"] == {"delivered": True, "delivery_ms": 42}
        assert full_reads.count == 0
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0

        # Case 2: append assistant row and write display_metadata BEFORE the next read (row id > old top).
        append_messages_batch(db, tip, [{"role": "assistant", "content": "pre-read stamped"}])
        newest_id = get_messages(db, tip)[-1]["id"]
        _set_display_metadata(db, newest_id, {"delivered": True, "timing_ms": 17})

        after_new = _fresh_raw_page(path, tip)
        assert after_new[-1]["id"] == newest_id
        assert after_new[-1]["display_metadata"] == {"delivered": True, "timing_ms": 17}
        assert full_reads.count == 0
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0


def test_codex_reasoning_items_and_checkpoint_prune_extend(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        append_messages_batch(db, tip, [{
            "role": "assistant",
            "content": "checkpoint carrier 1",
            "codex_reasoning_items": [
                {"type": "compaction", "encrypted_content": "ckpt-1"},
                {"type": "reasoning", "summary": "step 1"},
            ],
        }])
        _assert_pages_match(db, tip, full_reads)
        older_id = get_messages(db, tip)[-1]["id"]
        full_reads.reset()

        # Appending a newer checkpoint carrier triggers real _drop_shadowed_checkpoint_rows on older_id.
        append_messages_batch(db, tip, [{
            "role": "assistant",
            "content": "checkpoint carrier 2",
            "codex_reasoning_items": [{"type": "compaction", "encrypted_content": "ckpt-2"}],
        }])
        page = _fresh_page(path, tip)
        assert page[-1][3] == "checkpoint carrier 2"
        assert full_reads.count == 0

        # Explicit codex_reasoning_items rewrite on the new row and on an older row of the tip.
        newer_id = page[-1][0]
        _set_codex_reasoning(db, newer_id, [{"type": "reasoning", "summary": "final"}])
        _set_codex_reasoning(db, older_id, None)
        assert _fresh_page(path, tip) == page
        assert full_reads.count == 0
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0


def test_content_rewrite_on_old_tip_row_rebuilds_and_shows_new_content(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        append_messages_batch(db, tip, [{"role": "user", "content": "raw submit"}])
        _assert_pages_match(db, tip, full_reads)
        user_id = get_messages(db, tip)[-1]["id"]
        full_reads.reset()

        assert set_user_message_content(db, tip, user_id, "expanded @file prompt") == 1
        page = _fresh_page(path, tip)
        assert page[-1][3] == "expanded @file prompt"
        assert full_reads.count == 1
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1


def test_model_only_flag_on_old_tip_row_rebuilds_and_hides_row(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        append_messages_batch(db, tip, [{"role": "assistant", "content": "soon hidden"}])
        before = _assert_pages_match(db, tip, full_reads)
        target_id = before[-1][0]
        full_reads.reset()

        _set_display_metadata(db, target_id, {"model_only": True})
        after = _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        assert target_id not in [row[0] for row in after]
        assert len(after) == len(before) - 1

        # Flipping model_only back to False rebuilds again and restores the row.
        full_reads.reset()
        _set_display_metadata(db, target_id, {"model_only": False})
        restored = _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        assert target_id in [row[0] for row in restored]


def test_rewrite_in_non_tip_segment_rebuilds(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        non_tip_row = next(m for m in get_messages(db, "s0") if m["content"] == "ask 0.0")
        full_reads.reset()

        db._execute_write(
            lambda conn: conn.execute("UPDATE messages SET content = 'EDITED ANCESTOR' WHERE id = ?", (non_tip_row["id"],))
        )
        after = _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        assert any(row[0] == non_tip_row["id"] and row[3] == "EDITED ANCESTOR" for row in after)


def test_compaction_rotation_uses_new_lineage_key_and_extends_after(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        full_reads.reset()

        _rotate(db, tip, "s3")
        append_messages_batch(db, "s3", _turns(3))
        after = _assert_pages_match(db, "s3", full_reads)
        assert full_reads.count == 1
        assert after[-1][3] == "reply 3.2"

        full_reads.reset()
        append_messages_batch(db, "s3", [{"role": "user", "content": "post-rotation"}])
        assert _fresh_page(path, "s3")[-1][3] == "post-rotation"
        assert full_reads.count == 0
        _assert_pages_match(db, "s3", full_reads)
        assert full_reads.count == 0


def test_rewind_rebuilds_and_matches(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        before = _assert_pages_match(db, tip, full_reads)
        target_id = next(m["id"] for m in get_messages(db, tip) if m["content"] == "ask 2.2")
        full_reads.reset()

        rewind_to_message(db, tip, target_id)
        after = _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        assert "ask 2.2" not in [row[3] for row in after]
        assert len(after) < len(before)


# ── Fix 1 item 6 unit test: display_metadata normalization in digest ────────


def test_digest_display_metadata_normalizes_non_user_rows_only():
    cols = lineage_page._DIGEST_COLUMN_ORDER

    def digest_of(role: str, display_metadata):
        row: dict[str, Any] = {col: None for col in cols}
        row.update({"id": 7, "session_id": "s2", "role": role, "content": "hello",
                    "timestamp": 1700000000.0, "active": 1, "compacted": 0, "_compressed_summary": 0,
                    "display_metadata": display_metadata})
        h = hashlib.blake2b(digest_size=16)
        lineage_page._digest_update(h, [row], cols, positional=False)
        return h.digest()

    # Assistant rows: non-model_only metadata changes keep the digest identical.
    base_asst = digest_of("assistant", None)
    assert digest_of("assistant", json.dumps({"delivered": True, "ms": 12})) == base_asst
    assert digest_of("assistant", json.dumps({"delivered": True, "model_only": False})) == base_asst
    # Toggling model_only changes the assistant digest.
    assert digest_of("assistant", json.dumps({"model_only": True})) != base_asst
    assert digest_of("assistant", json.dumps({"delivered": True, "model_only": True})) != base_asst

    # Tool rows follow the same non-user rule.
    base_tool = digest_of("tool", None)
    assert digest_of("tool", json.dumps({"duration_ms": 99})) == base_tool
    assert digest_of("tool", json.dumps({"model_only": True})) != base_tool

    # User rows: ANY display_metadata change changes the digest (feeds split_user_originated_turn).
    base_user = digest_of("user", None)
    meta_a = digest_of("user", json.dumps({"delivered": True}))
    meta_b = digest_of("user", json.dumps({"reactions": [{"emoji": "ok"}]}))
    assert meta_a != base_user
    assert meta_b != base_user
    assert meta_b != meta_a


# ── 2. Live-turn simulation (20 iterations, 1 total full read) ──────────────


def test_live_turn_simulation_rebuilds_only_once(tmp_path, full_reads):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        # Seed an initial assistant checkpoint row in the tip and warm the projection (the 1 cold build).
        append_messages_batch(db, tip, [{
            "role": "assistant",
            "content": "initial reply",
            "codex_reasoning_items": [{"type": "compaction", "encrypted_content": "ckpt-init"}],
        }])
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1
        assistant_ids = [get_messages(db, tip)[-1]["id"]]

        for i in range(20):
            # 1. Append user row -> page.
            append_messages_batch(db, tip, [{"role": "user", "content": f"turn {i} ask"}])
            page = _fresh_page(path, tip, limit=10, offset=0, latest=True)
            user_id = page[-1][0]
            assert page[-1][3] == f"turn {i} ask"

            # 2. Backfill api_content on the (now old) user row -> page.
            assert set_message_api_content(db, tip, user_id, f"turn {i} ask", f"<ctx>turn {i} ask") == 1
            assert _fresh_page(path, tip, limit=10, offset=0, latest=True) == page

            # 3. Append assistant row + set non-model_only display_metadata + codex_reasoning_items -> page.
            append_messages_batch(db, tip, [{
                "role": "assistant",
                "content": f"turn {i} reply",
                "codex_reasoning_items": [
                    {"type": "compaction", "encrypted_content": f"ckpt-{i}"},
                    {"type": "reasoning", "summary": f"r-{i}"},
                ],
            }])
            new_asst_id = get_messages(db, tip)[-1]["id"]
            _set_display_metadata(db, new_asst_id, {"delivered": True, "turn": i})
            _set_codex_reasoning(db, new_asst_id, [
                {"type": "compaction", "encrypted_content": f"ckpt-{i}-updated"},
                {"type": "reasoning", "summary": f"r-{i}"},
            ])
            raw_page = _fresh_raw_page(path, tip, limit=10, offset=0, latest=True)
            assert raw_page[-1]["id"] == new_asst_id
            assert raw_page[-1]["display_metadata"] == {"delivered": True, "turn": i}

            # 4. Prune codex_reasoning_items on an older assistant row -> page.
            _set_codex_reasoning(db, assistant_ids[-1], [{"type": "reasoning", "summary": "pruned"}])
            assistant_ids.append(new_asst_id)
            assert _view(_fresh_raw_page(path, tip, limit=10, offset=0, latest=True)) == _view(raw_page)

            _assert_pages_match(db, tip, full_reads)

        assert full_reads.count == 1


# ── 3. Concurrency: 8 threads cold -> 1 full rebuild (single-flight) ────────


def test_concurrent_cold_reads_single_flight_one_full_rebuild(tmp_path, full_reads, monkeypatch):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        with full_reads.oracle():
            expected = _sliced(_full(db, tip), 4, 0, True)

    real_dedupe = store_messages._dedupe_display_generations
    hold = threading.Event()

    def slow_dedupe(holder, rows, **kwargs):
        hold.wait(timeout=0.3)
        return real_dedupe(holder, rows, **kwargs)

    monkeypatch.setattr(store_messages, "_dedupe_display_generations", slow_dedupe)

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[list | None] = [None] * n_threads
    errors: list[BaseException] = []

    def worker(idx: int) -> None:
        try:
            with StoreHolder(path, read_only=True) as reader:
                barrier.wait(timeout=5.0)
                results[idx] = _page(reader, tip, 4, 0, True)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()

    assert errors == []
    assert all(res == expected for res in results)
    assert full_reads.count == 1


# ── 4. Bound: 5 cold lineages concurrently -> max concurrent rebuilds <= 2 ──


def test_concurrent_cold_rebuilds_across_lineages_bounded_to_two(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    tips: list[str] = []
    expected: dict[str, list] = {}
    with StoreHolder(path) as db:
        for idx in range(5):
            tip = _named_lineage(db, f"lin{idx}_", segments=2)
            tips.append(tip)
            expected[tip] = _sliced(_full(db, tip), 4, 0, True)

    real_dedupe = store_messages._dedupe_display_generations
    counter_lock = threading.Lock()
    active = 0
    peak = 0
    hold = threading.Event()

    def bounded_dedupe(holder, rows, **kwargs):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        try:
            hold.wait(timeout=0.25)
            return real_dedupe(holder, rows, **kwargs)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(store_messages, "_dedupe_display_generations", bounded_dedupe)

    barrier = threading.Barrier(len(tips))
    results: dict[str, list] = {}
    errors: list[BaseException] = []

    def worker(tip: str) -> None:
        try:
            with StoreHolder(path, read_only=True) as reader:
                barrier.wait(timeout=5.0)
                results[tip] = _page(reader, tip, 4, 0, True)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tip,)) for tip in tips]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
        assert not t.is_alive()

    assert errors == []
    assert results == expected
    assert peak <= getattr(lineage_page, "_MAX_CONCURRENT_REBUILDS", 2)


# ── 5. Failure: builder raises -> waiters recover, no slot leak ─────────────


def test_builder_failure_wakes_waiters_and_does_not_leak_slot(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        expected = _sliced(_full(db, tip), 4, 0, True)

    real_dedupe = store_messages._dedupe_display_generations
    call_lock = threading.Lock()
    calls = 0
    hold = threading.Event()

    def failing_first_dedupe(holder, rows, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            hold.wait(timeout=0.2)
            raise RuntimeError("injected rebuild failure")
        return real_dedupe(holder, rows, **kwargs)

    monkeypatch.setattr(store_messages, "_dedupe_display_generations", failing_first_dedupe)

    n_threads = 6
    barrier = threading.Barrier(n_threads)
    results: list[list] = []
    errors: list[BaseException] = []
    res_lock = threading.Lock()

    def worker() -> None:
        try:
            with StoreHolder(path, read_only=True) as reader:
                barrier.wait(timeout=5.0)
                page = _page(reader, tip, 4, 0, True)
            with res_lock:
                results.append(page)
        except BaseException as exc:
            with res_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
        assert not t.is_alive()

    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert len(results) == n_threads - 1
    assert all(page == expected for page in results)
    assert not lineage_page._inflight

    # Verify zero slot leak: both rebuild slots can be acquired simultaneously right after.
    acquired = 0
    try:
        for _ in range(lineage_page._MAX_CONCURRENT_REBUILDS):
            assert lineage_page._rebuild_slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            lineage_page._rebuild_slots.release()


def test_wait_timeout_builds_independently_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        expected = _sliced(_full(db, tip), 4, 0, True)

    real_dedupe = store_messages._dedupe_display_generations
    builder_started = threading.Event()
    release_builder = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def stuck_first_dedupe(holder, rows, **kwargs):
        nonlocal calls
        with call_lock:
            calls += 1
            is_first = calls == 1
        if is_first:
            builder_started.set()
            release_builder.wait(timeout=5.0)
        return real_dedupe(holder, rows, **kwargs)

    monkeypatch.setattr(store_messages, "_dedupe_display_generations", stuck_first_dedupe)
    monkeypatch.setattr(lineage_page, "_REBUILD_WAIT_TIMEOUT_S", 0.05)

    first_res: list = []
    first_err: list[BaseException] = []

    def first_worker() -> None:
        try:
            with StoreHolder(path, read_only=True) as reader:
                first_res.append(_page(reader, tip, 4, 0, True))
        except BaseException as exc:
            first_err.append(exc)

    t1 = threading.Thread(target=first_worker)
    t1.start()
    assert builder_started.wait(timeout=5.0)

    try:
        with StoreHolder(path, read_only=True) as reader2:
            second_page = _page(reader2, tip, 4, 0, True)
        assert second_page == expected
    finally:
        release_builder.set()
        t1.join(timeout=5.0)

    assert not t1.is_alive()
    assert first_err == []
    assert first_res == [expected]
    assert not lineage_page._inflight
    acquired = 0
    try:
        for _ in range(lineage_page._MAX_CONCURRENT_REBUILDS):
            assert lineage_page._rebuild_slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            lineage_page._rebuild_slots.release()


def test_ioerr_retry_during_full_rebuild_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        expected = _sliced(_full(db, tip), 4, 0, True)

    real_with_removed = store_messages._with_removed_identities
    attempts = 0

    def flaky_with_removed(holder, rows, session_ids, conn=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_with_removed(holder, rows, session_ids, conn)

    monkeypatch.setattr(store_messages, "_with_removed_identities", flaky_with_removed)

    with StoreHolder(path, read_only=True) as reader:
        assert _page(reader, tip, 4, 0, True) == expected
    assert attempts == 2
    assert not lineage_page._inflight
    acquired = 0
    try:
        for _ in range(lineage_page._MAX_CONCURRENT_REBUILDS):
            assert lineage_page._rebuild_slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            lineage_page._rebuild_slots.release()


def test_max_verify_rows_exceeded_rebuilds_cleanly(tmp_path, full_reads, monkeypatch):
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        append_messages_batch(db, tip, [{"role": "user", "content": "over verify cap"}])
        _assert_pages_match(db, tip, full_reads)
        user_id = get_messages(db, tip)[-1]["id"]
        full_reads.reset()

        monkeypatch.setattr(lineage_page, "_MAX_VERIFY_ROWS", 2)
        assert set_message_api_content(db, tip, user_id, "over verify cap", "<ctx>over verify cap") == 1
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 1


# ── 6. Guard: every column read by the projection is in _DIGEST_COLUMNS ─────


class _RecordingRow(Mapping):
    """Mapping wrapper around a SQLite result row that records every column name accessed."""

    def __init__(self, cols: tuple[str, ...], values: tuple[Any, ...], accessed: set[str]) -> None:
        self._cols = cols
        self._values = values
        self._data = dict(zip(cols, values))
        self._accessed = accessed

    def __getitem__(self, key: str | int):
        if isinstance(key, int):
            col = self._cols[key]
            self._accessed.add(col)
            return self._values[key]
        self._accessed.add(key)
        return self._data[key]

    def get(self, key: str, default=None):
        self._accessed.add(key)
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def test_projection_column_guard_covers_all_read_columns(tmp_path):
    """Every column read by _with_removed_identities, _dedupe_display_generations, and _identities
    must live in lineage_page._DIGEST_COLUMNS | {'id'} so no projection input can be omitted."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db, segments=2)

        # Add tool-calling assistant + tool result + compacted original tool result + later live copy,
        # model_only row, composite compaction carrier, absorbed_message_uids, and finally a turn to rewind
        # (appended LAST so rewinding it leaves the earlier representative rows active for dedupe).
        append_messages_batch(db, tip, [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "tool_name": "search",
                "content": "full tool result",
                "message_uid": "uid-tool-1",
            },
            {
                "role": "user",
                "content": "model-only hidden prompt",
                "display_metadata": {"model_only": True},
            },
        ])
        msgs = get_messages(db, tip)
        tool_row = next(m for m in msgs if m["role"] == "tool")

        # Mark the original tool row compacted and append a later live copy with the same uid & timestamp
        # so later_tool_copy_ids exercises both its archived-original and later-copy branches.
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (tool_row["id"],)
        ))
        append_messages_batch(db, tip, [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "tool_name": "search",
                "content": "[pruned tool stub]",
                "timestamp": tool_row["timestamp"],
                "message_uid": "uid-tool-1",
            },
            {
                "role": "user",
                "content": "[CONTEXT SUMMARY]\nsummary body\n[END CONTEXT SUMMARY]\nlive ask after summary",
                "_compressed_summary": True,
                "absorbed_message_uids": ["uid-absorbed-a", "uid-absorbed-b"],
            },
            {
                "role": "user",
                "content": "to be rewound",
            },
            {
                "role": "assistant",
                "content": "reply to be rewound",
            },
        ])
        rewind_target = next(m["id"] for m in get_messages(db, tip) if m["content"] == "to be rewound")
        # Rewind produces removed rows (active=0, compacted=0); null display_identity on one of them
        # to also exercise the legacy fallback branch in _dedupe_display_generations.
        rewind_to_message(db, tip, rewind_target)
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET display_identity = NULL WHERE id = ?", (rewind_target,)
        ))

        ids = _resume_lineage_ids(db, tip)
        marks = ",".join("?" * len(ids))
        sql = f"SELECT * FROM messages WHERE session_id IN ({marks}) AND (active = 1 OR compacted = 1) ORDER BY id ASC"
        accessed: set[str] = set()
        merge_accessed: set[str] = set()

        def recording_factory(cursor, values):
            cols = tuple(desc[0] for desc in cursor.description)
            return _RecordingRow(cols, values, accessed)

        with db._read_ctx() as conn:
            prev_factory = conn.row_factory
            conn.row_factory = recording_factory
            try:
                display_rows = conn.execute(sql, ids).fetchall()
                # Both display_rows and the removed rows queried inside _with_removed_identities are
                # _RecordingRow instances so any column read during the merge itself is recorded.
                merged = store_messages._with_removed_identities(db, display_rows, ids, conn)
                merge_accessed.update(accessed)
            finally:
                conn.row_factory = prev_factory

        assert all(isinstance(row, _RecordingRow) for row in merged)
        assert len(merged) > len(display_rows), "removed rows must be merged by _with_removed_identities"
        removed_in_merged = [row for row in merged if row._data["active"] == 0 and row._data["compacted"] == 0]
        assert removed_in_merged, "representative lineage must include removed rows"
        assert "id" in merge_accessed, "_with_removed_identities must have read recording rows during merge"

        allowed = lineage_page._DIGEST_COLUMNS | {"id"}
        removed_cols = set(removed_in_merged[0].keys())
        assert removed_cols <= allowed, f"uncovered _REMOVED_IDENTITIES_SQL columns: {removed_cols - allowed}"
        assert merge_accessed <= allowed, f"_with_removed_identities read uncovered columns: {merge_accessed - allowed}"

        shown = store_messages._dedupe_display_generations(db, merged, drop_removed=True)
        assert shown
        lineage_page._identities(merged)

        # Confirm the fixture actually exercised the subtle branches (slots, summary carrier, tool calls, uids).
        expected_exercised = {
            "id", "session_id", "role", "content", "timestamp", "tool_call_id", "tool_calls", "tool_name",
            "display_kind", "display_metadata", "display_identity", "display_order", "message_uid",
            "absorbed_message_uids", "_compressed_summary", "active", "compacted",
        }
        assert expected_exercised <= accessed, f"fixture did not exercise: {expected_exercised - accessed}"
        assert accessed <= allowed, f"projection read columns missing from _DIGEST_COLUMNS: {accessed - allowed}"
        # Non-projection columns must stay out of _DIGEST_COLUMNS so live-turn writes do not invalidate it.
        non_projection = {
            "api_content",
            "codex_reasoning_items",
            "codex_message_items",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
            "token_count",
            "finish_reason",
        }
        assert not (lineage_page._DIGEST_COLUMNS & non_projection)


# ── 7. Single-flight edge cases (caller snapshots, CAS publish, takeover, oversized growth) ──


def _assert_all_rebuild_slots_free() -> None:
    acquired = 0
    try:
        for _ in range(lineage_page._MAX_CONCURRENT_REBUILDS):
            assert lineage_page._rebuild_slots.acquire(blocking=False)
            acquired += 1
    finally:
        for _ in range(acquired):
            lineage_page._rebuild_slots.release()


class _SqlRecordingConn:
    """Proxy over sqlite3.Connection that records every SQL string passed to execute()."""

    def __init__(self, conn, queries: list[str]) -> None:
        self._conn = conn
        self._queries = queries

    def execute(self, sql: str, *args, **kwargs):
        self._queries.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_snapshot_read_does_not_block_when_all_rebuild_slots_held(tmp_path):
    """Inside transcript_read_snapshot (caller owns the transaction), never block on rebuild slots."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        expected = _sliced(_full(db, tip), 4, 0, True)
        lineage_page._projections.clear()

        slot_count = lineage_page._MAX_CONCURRENT_REBUILDS
        held_barrier = threading.Barrier(slot_count + 1)
        release_slots = threading.Event()

        def hold_slot() -> None:
            assert lineage_page._rebuild_slots.acquire(timeout=5.0)
            try:
                held_barrier.wait(timeout=5.0)
                release_slots.wait(timeout=10.0)
            finally:
                lineage_page._rebuild_slots.release()

        holders = [threading.Thread(target=hold_slot) for _ in range(slot_count)]
        for t in holders:
            t.start()

        snap_result: list[Any] = []
        snap_errors: list[BaseException] = []
        snap_elapsed: list[float] = []
        snap_consistent: list[bool] = []

        def run_snapshot_read() -> None:
            try:
                with db.transcript_read_snapshot() as snap:
                    assert snap.pinned
                    t0 = time.monotonic()
                    page = _page(db, tip, 4, 0, True)
                    snap_elapsed.append(time.monotonic() - t0)
                    snap_consistent.append(snap())
                    snap_result.append(page)
            except BaseException as exc:
                snap_errors.append(exc)

        snap_thread = threading.Thread(target=run_snapshot_read)
        try:
            held_barrier.wait(timeout=5.0)
            assert not lineage_page._rebuild_slots.acquire(blocking=False)
            snap_thread.start()
            # Must finish promptly BEFORE the slot holders are released.
            snap_thread.join(timeout=1.5)
            finished_while_slots_held = not snap_thread.is_alive()
        finally:
            release_slots.set()
            for t in holders:
                t.join(timeout=5.0)
            snap_thread.join(timeout=5.0)

        assert finished_while_slots_held, "snapshot read blocked on rebuild slots while holding caller txn"
        assert snap_errors == []
        assert snap_consistent == [True]
        assert snap_elapsed and snap_elapsed[0] < 0.5
        assert snap_result == [expected]
        assert not lineage_page._inflight
        _assert_all_rebuild_slots_free()


def test_snapshot_read_does_not_wait_on_inflight_builder(tmp_path, monkeypatch):
    """Inside transcript_read_snapshot (caller owns the transaction), never wait on or register a flight."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        expected = _sliced(_full(db, tip), 4, 0, True)
        lineage_page._projections.clear()
        key = (lineage_page._store_identity(db), tuple(_resume_lineage_ids(db, tip)))

        real_dedupe = store_messages._dedupe_display_generations
        builder_started = threading.Event()
        release_builder = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def stall_first_dedupe(holder, rows, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                is_first = calls == 1
            if is_first:
                builder_started.set()
                release_builder.wait(timeout=5.0)
            return real_dedupe(holder, rows, **kwargs)

        monkeypatch.setattr(store_messages, "_dedupe_display_generations", stall_first_dedupe)
        monkeypatch.setattr(lineage_page, "_REBUILD_WAIT_TIMEOUT_S", 2.0)

        first_res: list[Any] = []
        first_err: list[BaseException] = []

        def first_builder() -> None:
            try:
                with StoreHolder(path, read_only=True) as reader:
                    first_res.append(_page(reader, tip, 4, 0, True))
            except BaseException as exc:
                first_err.append(exc)

        t1 = threading.Thread(target=first_builder)
        t1.start()
        try:
            assert builder_started.wait(timeout=5.0)
            orig_flight = lineage_page._inflight.get(key)
            assert orig_flight is not None

            wait_called = threading.Event()
            orig_wait = orig_flight.wait

            def spy_wait(*args, **kwargs):
                wait_called.set()
                return orig_wait(*args, **kwargs)

            orig_flight.wait = spy_wait  # type: ignore[method-assign]

            with db.transcript_read_snapshot() as snap:
                assert snap.pinned
                t0 = time.monotonic()
                snap_page = _page(db, tip, 4, 0, True)
                elapsed = time.monotonic() - t0
                assert snap()
                inflight_after_snap = lineage_page._inflight.get(key)
        finally:
            release_builder.set()
            t1.join(timeout=5.0)

        assert not t1.is_alive()
        assert not wait_called.is_set(), "snapshot read waited on another thread's flight inside caller txn"
        assert elapsed < 0.5, f"snapshot read took {elapsed:.3f}s (expected prompt inline build)"
        # Snapshot read must not have replaced or cleared the active flight.
        assert inflight_after_snap is orig_flight
        assert snap_page == expected
        assert first_err == []
        assert first_res == [expected]
        assert not lineage_page._inflight
        _assert_all_rebuild_slots_free()


def test_superseded_builder_never_overwrites_newer_projection(tmp_path, full_reads, monkeypatch):
    """A timed-out builder finishing after its replacement published must not overwrite the cache."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        with full_reads.oracle():
            expected_old = _sliced(_full(db, tip), 4, 0, True)
        lineage_page._projections.clear()
        full_reads.reset()
        key = (lineage_page._store_identity(db), tuple(_resume_lineage_ids(db, tip)))

        real_dedupe = store_messages._dedupe_display_generations
        builder_started = threading.Event()
        release_builder = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def stall_first_dedupe(holder, rows, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                is_first = calls == 1
            if is_first:
                builder_started.set()
                release_builder.wait(timeout=5.0)
            return real_dedupe(holder, rows, **kwargs)

        monkeypatch.setattr(store_messages, "_dedupe_display_generations", stall_first_dedupe)
        monkeypatch.setattr(lineage_page, "_REBUILD_WAIT_TIMEOUT_S", 0.15)

        t1_res: list[Any] = []
        t1_err: list[BaseException] = []

        def stalled_builder() -> None:
            try:
                with StoreHolder(path, read_only=True) as reader:
                    t1_res.append(_page(reader, tip, 4, 0, True))
            except BaseException as exc:
                t1_err.append(exc)

        t1 = threading.Thread(target=stalled_builder)
        t1.start()
        try:
            assert builder_started.wait(timeout=5.0)

            # Advance the transcript AND bump transcript_revision while t1 is stalled on the older snapshot.
            append_messages_batch(db, tip, [{"role": "user", "content": "newer turn"}])
            new_user_id = get_messages(db, tip)[-1]["id"]
            assert set_user_message_content(db, tip, new_user_id, "newer turn revised") == 1
            with full_reads.oracle():
                expected_new = _sliced(_full(db, tip), 4, 0, True)

            # Replacement reader times out on t1's flight, takes over, and publishes the newer projection.
            replacement_page = _fresh_page(path, tip, 4, 0, True)
            assert replacement_page == expected_new
            kept_after_replacement = lineage_page._projections[key]
            assert kept_after_replacement.revisions[-1] >= 1
        finally:
            release_builder.set()
            t1.join(timeout=5.0)

        assert not t1.is_alive()
        assert t1_err == []
        # The superseded builder still returns its own snapshot's exact page.
        assert t1_res == [expected_old]
        # Cache must still hold the replacement's newer projection (revisions and stamp intact).
        assert lineage_page._projections[key] is kept_after_replacement
        assert lineage_page._projections[key].revisions == kept_after_replacement.revisions
        assert lineage_page._projections[key].stamp == kept_after_replacement.stamp

        # Next unchanged poll hits the kept replacement projection with 0 full reads.
        full_reads.reset()
        assert _fresh_page(path, tip, 4, 0, True) == expected_new
        assert full_reads.count == 0
        assert not lineage_page._inflight
        _assert_all_rebuild_slots_free()


def test_inline_snapshot_and_growth_publish_respect_cas(tmp_path, full_reads, monkeypatch):
    """Inline snapshot rebuilds and growth extensions publish only if the cached entry is unchanged."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        with full_reads.oracle():
            expected_v0 = _sliced(_full(db, tip), 4, 0, True)
        lineage_page._projections.clear()
        full_reads.reset()
        key = (lineage_page._store_identity(db), tuple(_resume_lineage_ids(db, tip)))

        # 1. Inline snapshot rebuild (no flight) started from None must not overwrite a newer published entry.
        real_dedupe = store_messages._dedupe_display_generations
        snap_in_dedupe = threading.Event()
        release_snap = threading.Event()
        stall_next = [True]

        def stall_snap_dedupe(holder, rows, **kwargs):
            if stall_next[0]:
                stall_next[0] = False
                snap_in_dedupe.set()
                release_snap.wait(timeout=5.0)
            return real_dedupe(holder, rows, **kwargs)

        monkeypatch.setattr(store_messages, "_dedupe_display_generations", stall_snap_dedupe)
        snap_res: list[Any] = []
        snap_err: list[BaseException] = []

        def inline_worker() -> None:
            try:
                with db.transcript_read_snapshot() as snap:
                    assert snap.pinned
                    snap_res.append(_page(db, tip, 4, 0, True))
            except BaseException as exc:
                snap_err.append(exc)

        t_snap = threading.Thread(target=inline_worker)
        t_snap.start()
        try:
            assert snap_in_dedupe.wait(timeout=5.0)
            append_messages_batch(db, tip, [{"role": "user", "content": "v1 while inline stalled"}])
            with full_reads.oracle():
                expected_v1 = _sliced(_full(db, tip), 4, 0, True)
            assert _fresh_page(path, tip, 4, 0, True) == expected_v1
            assert t_snap.is_alive(), "newer publish must land while inline snapshot builder is still stalled"
            kept_v1 = lineage_page._projections[key]
        finally:
            release_snap.set()
            t_snap.join(timeout=5.0)

        assert snap_err == []
        assert snap_res == [expected_v0]
        assert lineage_page._projections[key] is kept_v1

        # 2. Growth extension from kept_v1 -> v2 must not overwrite a newer v3 entry published mid-flight.
        monkeypatch.setattr(store_messages, "_dedupe_display_generations", real_dedupe)
        append_messages_batch(db, tip, [{"role": "user", "content": "v2 append"}])
        with full_reads.oracle():
            expected_v2 = _sliced(_full(db, tip), 4, 0, True)

        real_extended = lineage_page._extended
        injected_v3 = [False]
        kept_v3_holder: list[Any] = []

        def racing_extended(holder, conn, kept, stamp, ids):
            grown = real_extended(holder, conn, kept, stamp, ids)
            if not injected_v3[0] and grown is not None:
                injected_v3[0] = True
                # Publish a newer v3 projection before this growth call publishes its v2 projection.
                append_messages_batch(db, tip, [{"role": "assistant", "content": "v3 race winner"}])
                _fresh_page(path, tip, 4, 0, True)
                kept_v3_holder.append(lineage_page._projections[key])
            return grown

        monkeypatch.setattr(lineage_page, "_extended", racing_extended)
        v2_page = _fresh_page(path, tip, 4, 0, True)
        assert v2_page == expected_v2
        assert len(kept_v3_holder) == 1
        assert lineage_page._projections[key] is kept_v3_holder[0]
        full_reads.reset()
        _assert_pages_match(db, tip, full_reads)
        assert full_reads.count == 0


def test_staggered_three_readers_takeover_wakes_displaced_waiter(tmp_path, full_reads, monkeypatch):
    """When reader 2 times out and takes over flight E, reader 3 waiting on E wakes and finishes right
    after reader 2 publishes (not another timeout later), with 2 full reads total and no stale overwrite."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        with full_reads.oracle():
            expected_old = _sliced(_full(db, tip), 4, 0, True)
        lineage_page._projections.clear()
        full_reads.reset()
        key = (lineage_page._store_identity(db), tuple(_resume_lineage_ids(db, tip)))

        real_dedupe = store_messages._dedupe_display_generations
        builder_started = threading.Event()
        release_builder = threading.Event()
        call_lock = threading.Lock()
        calls = 0

        def stall_first_dedupe(holder, rows, **kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
                is_first = calls == 1
            if is_first:
                builder_started.set()
                release_builder.wait(timeout=5.0)
            return real_dedupe(holder, rows, **kwargs)

        monkeypatch.setattr(store_messages, "_dedupe_display_generations", stall_first_dedupe)
        monkeypatch.setattr(lineage_page, "_REBUILD_WAIT_TIMEOUT_S", 0.6)

        t1_res: list[Any] = []
        t1_err: list[BaseException] = []

        def reader1() -> None:
            try:
                t1_res.append(_fresh_page(path, tip, 4, 0, True))
            except BaseException as exc:
                t1_err.append(exc)

        t1 = threading.Thread(target=reader1)
        t1.start()

        r2_res: list[Any] = []
        r2_err: list[BaseException] = []
        r2_done_at: list[float] = []
        r3_res: list[Any] = []
        r3_err: list[BaseException] = []
        r3_done_at: list[float] = []

        def reader2() -> None:
            try:
                page = _fresh_page(path, tip, 4, 0, True)
                r2_done_at.append(time.monotonic())
                r2_res.append(page)
            except BaseException as exc:
                r2_err.append(exc)

        def reader3() -> None:
            try:
                page = _fresh_page(path, tip, 4, 0, True)
                r3_done_at.append(time.monotonic())
                r3_res.append(page)
            except BaseException as exc:
                r3_err.append(exc)

        t2 = threading.Thread(target=reader2)
        t3 = threading.Thread(target=reader3)
        try:
            assert builder_started.wait(timeout=5.0)
            orig_flight = lineage_page._inflight.get(key)
            assert orig_flight is not None

            # Advance DB while reader 1 is stalled so replacement publishes a newer projection.
            append_messages_batch(db, tip, [{"role": "user", "content": "takeover newer turn"}])
            new_uid = get_messages(db, tip)[-1]["id"]
            assert set_user_message_content(db, tip, new_uid, "takeover newer turn edited") == 1
            with full_reads.oracle():
                expected_new = _sliced(_full(db, tip), 4, 0, True)

            waiter_lock = threading.Lock()
            waiter_count = 0
            r2_waiting = threading.Event()
            r3_waiting = threading.Event()
            r3_saw_unset_event = [False]
            orig_wait = orig_flight.wait

            def tracked_wait(*args, **kwargs):
                nonlocal waiter_count
                with waiter_lock:
                    waiter_count += 1
                    idx = waiter_count
                if idx == 1:
                    r2_waiting.set()
                elif idx == 2:
                    r3_saw_unset_event[0] = not orig_flight.is_set()
                    r3_waiting.set()
                return orig_wait(*args, **kwargs)

            orig_flight.wait = tracked_wait  # type: ignore[method-assign]

            t2.start()
            assert r2_waiting.wait(timeout=5.0)
            # Stagger reader 3 partway through reader 2's wait timeout so reader 3 enters E.wait before takeover
            # and still has ~0.35s of its own timeout left when reader 2 takes over.
            stagger = threading.Event()
            stagger.wait(0.25)  # health: allow HX013 -- stagger reader 3 within reader 2's wait timeout
            t3.start()
            assert r3_waiting.wait(timeout=5.0)
            assert r3_saw_unset_event[0], "reader 3 must have queued on E before takeover"

            t2.join(timeout=5.0)
            t3.join(timeout=5.0)
            assert not t2.is_alive() and not t3.is_alive()

            kept_after_r2 = lineage_page._projections[key]
        finally:
            release_builder.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            t3.join(timeout=5.0)

        assert not t1.is_alive()
        assert r2_err == [] and r3_err == [] and t1_err == []
        assert r2_res == [expected_new]
        assert r3_res == [expected_new]
        assert t1_res == [expected_old]
        # Reader 3 must have been woken by takeover and finished right after reader 2 (not ~0.25s later).
        lag = r3_done_at[0] - r2_done_at[0]
        assert lag < 0.15, f"displaced waiter reader 3 lagged {lag:.3f}s behind replacement reader 2"
        # Exactly 2 full reads total: stalled reader 1 + replacement reader 2.
        assert full_reads.count == 2
        # Stalled builder must not have overwritten reader 2's cached projection.
        assert lineage_page._projections[key] is kept_after_r2
        full_reads.reset()
        assert _fresh_page(path, tip, 4, 0, True) == expected_new
        assert full_reads.count == 0
        assert not lineage_page._inflight
        _assert_all_rebuild_slots_free()


def test_oversized_growth_skips_select_star_and_rebuilds(tmp_path, full_reads, monkeypatch):
    """Appending more than _MAX_APPENDED_ROWS rows rejects growth before SELECT * of new rows."""
    path = tmp_path / "state.db"
    with StoreHolder(path) as db:
        tip = _lineage(db)
        _assert_pages_match(db, tip, full_reads)
        full_reads.reset()

        monkeypatch.setattr(lineage_page, "_MAX_APPENDED_ROWS", 5)

        def read_and_record_sql() -> tuple[list[Any], list[str]]:
            queries: list[str] = []
            page_rows: list[Any] = []
            with StoreHolder(path, read_only=True) as reader:
                real_read_ctx = reader._read_ctx

                @contextmanager
                def recording_read_ctx(*args, **kwargs):
                    with real_read_ctx(*args, **kwargs) as conn:
                        yield _SqlRecordingConn(conn, queries)

                monkeypatch.setattr(reader, "_read_ctx", recording_read_ctx)
                page_rows = _page(reader, tip, 4, 0, True)
            return page_rows, queries

        # Case 1: plain append of _MAX_APPENDED_ROWS + 1 rows (rejected cheaply via stamp delta).
        append_messages_batch(db, tip, [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"bulk {i}"}
            for i in range(6)
        ])
        page, queries = read_and_record_sql()
        appended_star = [q for q in queries if "SELECT *" in q.upper() and "ID > ?" in q.upper()]
        assert appended_star == [], f"oversized growth materialized new rows: {appended_star}"
        assert full_reads.count == 1
        full = _assert_pages_match(db, tip, full_reads)
        assert page == _sliced(full, 4, 0, True)
        assert full_reads.count == 1

        # Case 2: delete old rows + append > _MAX_APPENDED_ROWS rows so live_delta <= limit
        # (caught by the id-only LIMIT probe before SELECT *).
        full_reads.reset()
        tip_ids = [m["id"] for m in get_messages(db, tip)]
        delete_ids = tip_ids[:6]
        db._execute_write(
            lambda conn: conn.execute(
                f"DELETE FROM messages WHERE id IN ({','.join('?' * len(delete_ids))})", delete_ids
            )
        )
        append_messages_batch(db, tip, [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"churn {i}"}
            for i in range(8)
        ])
        page2, queries2 = read_and_record_sql()
        appended_star2 = [q for q in queries2 if "SELECT *" in q.upper() and "ID > ?" in q.upper()]
        assert appended_star2 == [], f"delete+append oversized growth materialized new rows: {appended_star2}"
        assert full_reads.count == 1
        full2 = _assert_pages_match(db, tip, full_reads)
        assert page2 == _sliced(full2, 4, 0, True)
        assert full_reads.count == 1
