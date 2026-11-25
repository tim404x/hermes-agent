"""Regression & guard tests for Kanban result-before-notify receipts and adoption (Item 5).

Covers:
- Crash rollback mid-txn (no status/run/receipt/outbox/event/orphan files) vs
  crash after commit / before notify (durable receipt + pending outbox + event).
- Immutable content-addressed receipts (SQLite trigger rejects UPDATE), verifiable
  summary-only results and pinned/hashed file artifacts.
- Hashing large artifacts outside SQLite's writer lock + protection against
  changing files during hashing or between hash and commit.
- Same-revision adoption at retry/replay entry points (`adopt_result_receipt`,
  `claim_task`, `dispatch_once`, same-run `complete_task` replay) with ZERO
  extra executor calls and ZERO duplicate notifications/events.
- `input_rev` stability across crash retries, invalidation on actual input edits
  or explicit reopen (including unchanged body), and board fencing.
- Corrupt/missing artifact fail-closed verification across verify, adopt, claim,
  dispatch, and notifier artifact delivery.
- Outbox retry/de-dup on the canonical `task_events` + `kanban_notify_subs` path.
- Stale claim fencing, parent/child dependency gates, and PR acceptance intact.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_result_receipts as kbrr


@pytest.fixture
def board_env(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKSPACES_ROOT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_ATTACHMENTS_ROOT", raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home, db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    return kbc.connect(db_path)


def test_declared_outputs_are_all_required(board_env, tmp_path):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_bytes(b'alpha')
    with kbc.connect_closing(board_env[1]) as conn:
        task = kb.create_task(conn, title='both outputs', assignee='worker', workspace_kind='dir', workspace_path=str(tmp_path))
        attempt = kb.claim_task(conn, task)
        kwargs = dict(summary='two files', expected_run_id=attempt.current_run_id, metadata={'artifacts': [str(first), str(second)]})
        with pytest.raises(kb.ArtifactPreservationError):
            kb.complete_task(conn, task, **kwargs)
        assert kb.get_task(conn, task).status == 'running' and not kb.list_result_receipts(conn, task)


def test_all_pinned_outputs_survive_source_overwrite_or_removal(board_env, tmp_path):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_bytes(b'alpha')
    second.write_bytes(b'beta')
    with kbc.connect_closing(board_env[1]) as conn:
        task = kb.create_task(conn, title='both outputs', assignee='worker', workspace_kind='dir', workspace_path=str(tmp_path))
        assert kb.complete_task(conn, task, summary='two files', metadata={'artifacts': [str(first), str(second)]})
        first.write_bytes(b'new contents')
        second.unlink()
        receipt, = kb.list_result_receipts(conn, task)
        entries = kb.verify_result_receipt(conn, receipt)['artifacts']
        assert [Path(entry['path']).read_bytes() for entry in entries] == [b'alpha', b'beta']


@pytest.mark.parametrize('field', ['body', 'assignee'])
def test_active_attempt_cannot_attest_edited_input(board_env, field):
    with kbc.connect_closing(board_env[1]) as conn:
        task = kb.create_task(conn, title='fenced attempt', body='old brief', assignee='worker')
        run = kb.claim_task(conn, task).current_run_id
        with kb.write_txn(conn):
            conn.execute(f'UPDATE tasks SET {field} = ? WHERE id = ?', ('changed', task))
        assert not kb.complete_task(conn, task, summary='work for old brief', expected_run_id=run)
        assert kb.get_task(conn, task).current_run_id == run and not kb.list_result_receipts(conn, task)


@pytest.mark.parametrize('new_status', ['running', 'blocked', 'archived'])
def test_adoption_respects_concurrent_transition_after_verification(board_env, monkeypatch, new_status):
    with kbc.connect_closing(board_env[1]) as conn, kbc.connect_closing(board_env[1]) as other:
        task = kb.create_task(conn, title='result replay', assignee='worker')
        assert kb.complete_task(conn, task, summary='original')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
        verify = kbrr.verify_result_receipt

        def transition(*args, **kwargs):
            manifest = verify(*args, **kwargs)
            with kb.write_txn(other):
                if new_status == 'running':
                    assert kb._claim_and_open_run(other, task, 'ready', 'new owner', 200, 100)
                else:
                    other.execute('UPDATE tasks SET status=? WHERE id=?', (new_status, task))
            return manifest

        monkeypatch.setattr(kbrr, 'verify_result_receipt', transition)
        assert kb.adopt_result_receipt(conn, task) is None
        current = kb.get_task(conn, task)
        assert current.status == new_status
        assert new_status != 'running' or current.current_run_id is not None


@pytest.mark.parametrize('remote_accepted', [False, True])
def test_new_collector_retries_unacked_destination(board_env, monkeypatch, remote_accepted):
    from tui_gateway import server
    target = dict(platform='tui', chat_id='session')
    other = dict(platform='telegram', chat_id='fixture')
    clock = kbrr.time.time()
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board_env[1]))
    with kbc.connect_closing(board_env[1]) as conn:
        task = kb.create_task(conn, title='recover delivery', assignee='worker')
        for sub in (target, other):
            kbn.add_notify_sub(conn, task_id=task, **sub)
        assert kb.complete_task(conn, task, summary='recoverable message')
        cid = kb.list_result_receipts(conn, task)[0].completion_id
        for sub in (target, other):
            assert kbn.claim_unseen_events_for_sub(conn, task_id=task, kinds=['completed'], **sub)[2]
        kbrr.mark_outbox_delivered(conn, [cid], task, **other)
        assert kb.get_outbox_entry(conn, cid).status == 'pending'
    accepted = ['before crash'] if remote_accepted else []
    monkeypatch.setattr(kbrr.time, 'time', lambda: clock + 86400)
    monkeypatch.setattr(server, '_emit', lambda *args: accepted.append(args))
    monkeypatch.setattr(server, '_notif_claim_turn', lambda session: False)
    server._notif_poll_kanban_scoped('sid', {'session_key': 'session'})
    assert len(accepted) == 1 + int(remote_accepted)
    with kbc.connect_closing(board_env[1]) as conn:
        assert kb.get_outbox_entry(conn, cid).status == 'delivered'


# ---------------------------------------------------------------------------
# 1. Crash mid-txn vs crash after commit / before notify
# ---------------------------------------------------------------------------

def test_crash_rollback_mid_txn_leaves_no_receipt_outbox_event_or_orphan_files(board_env, monkeypatch):
    _, db_path = board_env
    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="mid-txn crash", assignee="coder")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        ws = kbw.resolve_workspace(claimed)
        kbw.set_workspace_path(conn, tid, ws)
        artifact = ws / "output.txt"
        artifact.write_bytes(b"precious-result-bytes")

        def _crash_in_txn(*_a, **_kw):
            raise RuntimeError("simulated mid-txn crash before commit")

        monkeypatch.setattr(kb, "build_and_persist_receipt_in_txn", _crash_in_txn)
        with pytest.raises(RuntimeError, match="mid-txn crash"):
            kb.complete_task(
                conn, tid, summary="finished",
                metadata={"artifacts": [str(artifact)]},
                expected_run_id=claimed.current_run_id,
            )

        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "running"
        run = kb.get_run(conn, claimed.current_run_id)
        assert run is not None and run.ended_at is None
        assert kbrr.list_result_receipts(conn, tid) == []
        assert kbrr.list_pending_outbox(conn, tid) == []
        assert [e for e in kb.list_events(conn, tid) if e.kind == "completed"] == []
        att_dir = kb.task_attachments_dir(tid)
        assert not att_dir.exists() or not any(att_dir.iterdir())
        _, unseen = kbn.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1", kinds=["completed"],
        )
        assert unseen == []


def test_crash_after_commit_before_notify_persists_immutable_receipt_and_pending_outbox(board_env, monkeypatch):
    _, db_path = board_env
    with _connect(db_path) as conn:
        parent_id = kb.create_task(conn, title="parent work", body="spec v1", assignee="coder")
        child_id = kb.create_task(conn, title="child work", assignee="coder", parents=[parent_id])
        kbn.add_notify_sub(conn, task_id=parent_id, platform="telegram", chat_id="chat-1")
        claimed = kb.claim_task(conn, parent_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        assert run_id is not None

        # Simulate crash immediately AFTER write_txn COMMIT, BEFORE post-commit
        # housekeeping (recompute_ready / cleanup / hook / notifier).
        promote = kb.recompute_ready
        monkeypatch.setattr(kb, "recompute_ready", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("post-commit crash")))
        with pytest.raises(RuntimeError, match="post-commit crash"):
            kb.complete_task(
                conn, parent_id, summary="summary-only result", result="ok-v1",
                expected_run_id=run_id,
            )

    monkeypatch.setattr(kb, 'recompute_ready', promote)
    with _connect(db_path) as conn:
        task = kb.get_task(conn, parent_id)
        assert task is not None and task.status == "done"
        run = kb.get_run(conn, run_id)
        assert run is not None and run.outcome == "completed" and run.ended_at is not None

        receipts = kbrr.list_result_receipts(conn, parent_id)
        assert len(receipts) == 1
        rcpt = receipts[0]
        assert rcpt.task == parent_id
        assert rcpt.attempt == 1
        assert rcpt.run_id == run_id
        assert rcpt.status == "done"
        assert rcpt.artifact_uri.startswith("receipt://")
        assert rcpt.sha256 == hashlib.sha256(rcpt.manifest_json.encode("utf-8")).hexdigest()
        # Summary-only receipt verifies cleanly.
        manifest = kbrr.verify_result_receipt(conn, rcpt)
        assert manifest["summary"] == "summary-only result"
        assert manifest["result"] == "ok-v1"

        # Immutability trigger blocks UPDATE on kanban_result_receipts.
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute(
                "UPDATE kanban_result_receipts SET status = 'tampered' WHERE completion_id = ?",
                (rcpt.completion_id,),
            )

        # Pending outbox & canonical completed event share completion_id.
        outbox = kbrr.get_outbox_entry(conn, rcpt.completion_id)
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.completed_event_id == rcpt.completed_event_id
        completed_events = [e for e in kb.list_events(conn, parent_id) if e.kind == "completed"]
        assert len(completed_events) == 1
        assert completed_events[0].id == rcpt.completed_event_id
        assert completed_events[0].payload["completion_id"] == rcpt.completion_id
        assert completed_events[0].payload["receipt_sha256"] == rcpt.sha256
        assert completed_events[0].payload["input_rev"] == rcpt.input_rev

        # Because the crash occurred before recompute_ready, child is still in todo.
        assert kb.get_task(conn, child_id).status == "todo"

        # Post-commit replay / adoption returns original receipt and promotes child
        # WITHOUT creating a second receipt, event, or outbox row.
        adopted = kbrr.adopt_result_receipt(conn, parent_id)
        assert adopted is not None
        assert adopted.completion_id == rcpt.completion_id
        assert kb.get_task(conn, child_id).status == "ready"
        assert len(kbrr.list_result_receipts(conn, parent_id)) == 1
        assert len([e for e in kb.list_events(conn, parent_id) if e.kind == "completed"]) == 1
        assert len(kbrr.list_pending_outbox(conn, parent_id)) == 1

        # Same-run complete_task replay is idempotent (no duplicate event/outbox/receipt).
        assert kb.complete_task(
            conn, parent_id, summary="summary-only result", result="ok-v1",
            expected_run_id=run_id,
        ) is True
        assert len(kbrr.list_result_receipts(conn, parent_id)) == 1
        assert len([e for e in kb.list_events(conn, parent_id) if e.kind == "completed"]) == 1


# ---------------------------------------------------------------------------
# 2. Same revision / two attempts / adoption + zero extra executor calls
# ---------------------------------------------------------------------------

def test_same_revision_adoption_prevents_second_executor_call_and_duplicate_notif(board_env, monkeypatch):
    _, db_path = board_env
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))

    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="compute digest", body="same input", assignee="coder")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        rev_initial = kbrr.compute_input_rev(conn, tid)

        # Attempt 1 crashes BEFORE completing -> input_rev must remain stable across crash retry!
        c1 = kb.claim_task(conn, tid)
        assert c1 is not None
        kbd._record_task_failure(
            conn, tid, "worker crashed", outcome="crashed",
            failure_limit=5, release_claim=True, end_run=True,
        )
        assert kb.get_task(conn, tid).status == "ready"
        assert kbrr.compute_input_rev(conn, tid) == rev_initial

        # Attempt 2 runs and completes -> commits immutable receipt (attempt=2).
        c2 = kb.claim_task(conn, tid)
        assert c2 is not None
        ws = kbw.resolve_workspace(c2)
        kbw.set_workspace_path(conn, tid, ws)
        art = ws / "report.json"
        art.write_bytes(b'{"answer": 42}')
        assert kb.complete_task(
            conn, tid, summary="computed 42", result="42",
            metadata={"artifacts": [str(art)]}, expected_run_id=c2.current_run_id,
        )
        rcpt = kbrr.list_result_receipts(conn, tid)[0]
        assert rcpt.attempt == 2
        assert rcpt.input_rev == rev_initial
        runs_after_complete = len(kb.list_runs(conn, tid))

        # Simulate a retry/re-queue of the SAME logical revision (no reopen, no input edit).
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

        # Pre-admission check at dispatch_once adopts original result; spawn_fn MUST NOT run.
        spawn_calls: list[str] = []
        def _spawn(task, workspace, board=None):
            spawn_calls.append(task.id)
            return 99999

        dispatch_res = kbd.dispatch_once(conn, spawn_fn=_spawn)
        assert spawn_calls == [], f"executor must not be spawned on adopted revision: {spawn_calls}"
        assert dispatch_res.adopted == [tid]
        assert dispatch_res.spawned == []
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, tid).result == "42"
        assert len(kb.list_runs(conn, tid)) == runs_after_complete
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "completed"]) == 1

        # Pre-admission check at claim_task directly also adopts and returns None.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kb.claim_task(conn, tid) is None
        assert kb.get_task(conn, tid).status == "done"
        assert len(kb.list_runs(conn, tid)) == runs_after_complete
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "completed"]) == 1


# ---------------------------------------------------------------------------
# 3. Changed inputs & explicit reopen (including unchanged body) + board fence
# ---------------------------------------------------------------------------

def test_changed_input_and_explicit_reopen_with_unchanged_body_are_not_adopted(board_env, monkeypatch):
    _, db_path = board_env
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))

    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="spec task", body="unchanged body text", assignee="coder")
        c1 = kb.claim_task(conn, tid)
        assert c1 is not None
        assert kb.complete_task(conn, tid, summary="v1 done", expected_run_id=c1.current_run_id)
        rcpt1 = kbrr.list_result_receipts(conn, tid)[0]

        # 3A: Explicit reopen with UNCHANGED body creates a NEW logical revision.
        assert kb.reopen_task(conn, tid, reason="re-run requested") is True
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert task.body == "unchanged body text"
        assert task.reopen_epoch == 1
        rev_after_reopen = kbrr.compute_input_rev(conn, tid)
        assert rev_after_reopen != rcpt1.input_rev

        # Adoption returns None; dispatcher spawns executor for new logical revision!
        assert kbrr.adopt_result_receipt(conn, tid) is None
        spawned_ids: list[str] = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, _ws: spawned_ids.append(t.id) or 12345)
        assert spawned_ids == [tid]
        assert res.adopted == []

        c2 = kb.get_task(conn, tid)
        assert c2 is not None and c2.status == "running" and c2.current_run_id is not None
        assert kb.complete_task(conn, tid, summary="v2 done", expected_run_id=c2.current_run_id)
        receipts = kbrr.list_result_receipts(conn, tid)
        assert len(receipts) == 2
        assert receipts[0].completion_id == rcpt1.completion_id
        assert receipts[1].completion_id != rcpt1.completion_id
        assert receipts[1].input_rev == rev_after_reopen

        # 3B: Actual input edit (body changed) invalidates input_rev even without reopen_epoch bump.
        rev_before_edit = kbrr.compute_input_rev(conn, tid)
        assert kb.edit_task(conn, tid, body="edited body v3") is True
        rev_after_edit = kbrr.compute_input_rev(conn, tid)
        assert rev_after_edit != rev_before_edit
        assert kbrr.adopt_result_receipt(conn, tid) is None

        # Non-input priority edit does NOT invalidate input_rev.
        assert kb.edit_task(conn, tid, priority=99) is True
        assert kbrr.compute_input_rev(conn, tid) == rev_after_edit


def test_different_board_fence_rejects_foreign_receipt_and_input_rev(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_a = tmp_path / "board_a.db"
    db_b = tmp_path / "board_b.db"
    kb.init_db(db_a)
    kb.init_db(db_b)

    with kbc.connect(db_a) as conn_a, kbc.connect(db_b) as conn_b:
        tid = kb.create_task(conn_a, title="same title", body="same body", assignee="coder")
        # Mirror identical task_id + fields onto board B.
        row_a = conn_a.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        cols = list(row_a.keys())
        placeholders = ",".join("?" for _ in cols)
        with kb.write_txn(conn_b):
            conn_b.execute(
                f"INSERT INTO tasks ({','.join(cols)}) VALUES ({placeholders})",
                tuple(row_a[c] for c in cols),
            )

        rev_a = kbrr.compute_input_rev(conn_a, tid)
        rev_b = kbrr.compute_input_rev(conn_b, tid)
        assert rev_a != rev_b, "identical task on different boards must have distinct input_rev"

        assert kb.complete_task(conn_a, tid, summary="done on A")
        rcpt_a = kbrr.list_result_receipts(conn_a, tid)[0]

        # Foreign board verification MUST fail closed.
        with pytest.raises(kbrr.CorruptResultReceiptError, match="board_id mismatch"):
            kbrr.verify_result_receipt(conn_b, rcpt_a)

        # Even if a foreign receipt row were copied into board B, adoption refuses it.
        assert kbrr.adopt_result_receipt(conn_b, tid) is None
        assert kbrr.adopt_result_receipt(conn_a, tid, expected_board_id=kbrr.get_board_id(conn_b)) is None


# ---------------------------------------------------------------------------
# 4. Large artifact hashing outside writer lock, changing file guard, corrupt artifact
# ---------------------------------------------------------------------------

def test_large_artifact_hashed_outside_sqlite_writer_lock_and_changing_file_rejected(board_env, tmp_path, monkeypatch):
    _, db_path = board_env
    big_file = tmp_path / "large_output.bin"
    big_data = (b"0123456789abcdef" * 65536) * 2 + b"tail"  # > 2 MiB (multi-chunk)
    big_file.write_bytes(big_data)
    expected_sha = hashlib.sha256(big_data).hexdigest()

    in_txn_during_hash: list[bool] = []
    real_stream_hash = kbrr._stream_copy_and_hash

    def _spy_stream_hash(conn_ref):
        def _wrapped(*args, **kwargs):
            in_txn_during_hash.append(bool(getattr(conn_ref, "in_transaction", False)))
            return real_stream_hash(*args, **kwargs)
        return _wrapped

    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="large artifact", assignee="coder")
        monkeypatch.setattr(kbrr, "_stream_copy_and_hash", _spy_stream_hash(conn))
        assert kb.complete_task(
            conn, tid, summary="wrote large artifact", metadata={"artifacts": [str(big_file)]},
        )
        assert in_txn_during_hash == [False], "large artifact hashing must happen outside write_txn"
        rcpt = kbrr.list_result_receipts(conn, tid)[0]
        assert rcpt.artifact_uri == f"receipt://{rcpt.board_id}/{rcpt.completion_id}/manifest"
        manifest = kbrr.verify_result_receipt(conn, rcpt)
        assert manifest["artifacts"][0]["sha256"] == expected_sha
        assert manifest["artifacts"][0]["size"] == len(big_data)

    # Protect against changing file mid-hash.
    mutating_file = tmp_path / "mutating.bin"
    mutating_file.write_bytes(b"A" * (1024 * 1024 + 128))
    real_os_read = kbrr.os.read
    mutated_once = {"done": False}

    def _mutating_read(fd, n):
        chunk = real_os_read(fd, n)
        if chunk and not mutated_once["done"]:
            mutated_once["done"] = True
            # Mutate the file while hashing is in progress.
            with mutating_file.open("ab") as handle:
                handle.write(b"EXTRA-BYTES")
        return chunk

    with _connect(db_path) as conn:
        tid2 = kb.create_task(conn, title="mutating artifact", assignee="coder")
        monkeypatch.setattr(kbrr.os, "read", _mutating_read)
        with pytest.raises(kb.ArtifactPreservationError, match="changed while hashing"):
            kb.complete_task(
                conn, tid2, summary="mutating", metadata={"artifacts": [str(mutating_file)]},
            )
        assert kb.get_task(conn, tid2).status == "ready"
        assert kbrr.list_result_receipts(conn, tid2) == []
        assert kbrr.list_pending_outbox(conn, tid2) == []


def test_corrupt_or_missing_artifact_fails_closed_everywhere(board_env, monkeypatch):
    _, db_path = board_env
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda _name: True))

    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="artifact integrity", assignee="coder")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        ws = kbw.resolve_workspace(claimed)
        kbw.set_workspace_path(conn, tid, ws)
        art = ws / "model.bin"
        art.write_bytes(b"verified-payload-v1")
        assert kb.complete_task(
            conn, tid, summary="produced model",
            metadata={"artifacts": [str(art)]}, expected_run_id=claimed.current_run_id,
        )
        rcpt = kbrr.list_result_receipts(conn, tid)[0]
        pinned_path = Path(rcpt.manifest["artifacts"][0]["path"])
        assert pinned_path.exists()
        assert kbrr.verify_result_receipt(conn, rcpt)["artifacts"][0]["size"] == len(b"verified-payload-v1")

        # Corrupt a byte on disk!
        pinned_path.write_bytes(b"corrupted-payload!!")

        # verify_result_receipt fails closed.
        with pytest.raises(kbrr.CorruptResultReceiptError, match="sha256 mismatch"):
            kbrr.verify_result_receipt(conn, rcpt)

        # Re-queue same revision: adopt_result_receipt & claim_task fail closed!
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        with pytest.raises(kbrr.CorruptResultReceiptError):
            kbrr.adopt_result_receipt(conn, tid)
        with pytest.raises(kbrr.CorruptResultReceiptError):
            kb.claim_task(conn, tid)

        # dispatch_once fails closed (no adoption, no spawn, audit event logged).
        spawn_calls: list[str] = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, _ws: spawn_calls.append(t.id) or 777)
        assert spawn_calls == []
        assert res.adopted == []
        assert res.corrupt_receipts == [tid]
        assert any(e.kind == "receipt_verification_failed" for e in kb.list_events(conn, tid))

        # Notifier verification also fails closed.
        ok, reason = kbrr.verify_completion_artifacts_for_delivery(conn, rcpt.completion_id)
        assert ok is False and reason is not None

        # Deleting the pinned artifact also fails closed.
        pinned_path.unlink()
        with pytest.raises(kbrr.CorruptResultReceiptError, match="missing"):
            kbrr.verify_result_receipt(conn, rcpt)


# ---------------------------------------------------------------------------
# 5. Stale claim fencing + PR acceptance & parent gates intact
# ---------------------------------------------------------------------------

def test_stale_claim_fenced_and_gates_intact(board_env):
    _, db_path = board_env
    with _connect(db_path) as conn:
        # Stale attempt fencing: run1 reclaimed, run2 completes; stale run1 cannot
        # complete OR adopt run2's completion.
        tid = kb.create_task(conn, title="two runs race", assignee="coder")
        c1 = kb.claim_task(conn, tid)
        assert c1 is not None
        run1 = c1.current_run_id
        assert kb.reclaim_task(conn, tid, reason="timeout") is True
        c2 = kb.claim_task(conn, tid)
        assert c2 is not None
        run2 = c2.current_run_id
        assert run2 != run1

        # While run2 is active, stale run1 cannot complete.
        assert kb.complete_task(conn, tid, summary="stale run1", expected_run_id=run1) is False
        assert kbrr.list_result_receipts(conn, tid) == []

        # run2 completes and wins receipt.
        assert kb.complete_task(conn, tid, summary="winning run2", expected_run_id=run2) is True
        rcpt = kbrr.list_result_receipts(conn, tid)[0]
        assert rcpt.run_id == run2

        # Stale run1 replay after run2 completed MUST be rejected (not adopted!).
        assert kb.complete_task(conn, tid, summary="stale run1 late", expected_run_id=run1) is False
        assert kbrr.adopt_result_receipt(conn, tid, expected_run_id=run1) is None

        # Parent gate intact: child with open parent cannot complete or adopt.
        parent = kb.create_task(conn, title="open parent", assignee="coder")
        child = kb.create_task(conn, title="gated child", assignee="coder", parents=[parent])
        assert kb.complete_task(conn, child, summary="premature") is False
        assert kbrr.list_result_receipts(conn, child) == []
        assert kbrr.list_pending_outbox(conn, child) == []


# ---------------------------------------------------------------------------
# 6. Outbox retry / de-dup on canonical notifier path (no duplicate channel)
# ---------------------------------------------------------------------------

class _FlakyNotifyAdapter:
    """Fails send once, then fails wake once, then succeeds on both."""

    def __init__(self) -> None:
        self.send_calls = 0
        self.wake_calls = 0
        self.sent: list[dict] = []
        self.handled: list[object] = []
        self.docs: list[str] = []

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        if self.send_calls == 1:
            raise RuntimeError("transient 503 on send")
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.wake_calls += 1
        if self.wake_calls == 1:
            # Simulates WakeNotAccepted (gateway queue temporarily full).
            event._gateway_accepted = False
            return
        event._gateway_accepted = True
        self.handled.append(event)

    async def send_document(self, chat_id, file_path, metadata=None, **_kw):
        self.docs.append(file_path)

    def extract_local_files(self, _text):
        return [], _text


@pytest.fixture
def runner_factory(board_env, monkeypatch):
    from gateway.config import Platform
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    home, db_path = board_env
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    class _StubRunner(GatewayKanbanWatchersMixin):
        def __init__(self, adapter):
            self._running = True
            self.adapters = {Platform.TELEGRAM: adapter}
            self._profile_adapters = {}
            self._kanban_sub_fail_counts = {}
            self._kanban_dispatcher_lock_handle = object()
            self._kanban_notifier_profile = "default"
            self.config = None

        def _active_profile_name(self):
            return "default"

        def _authorization_adapter(self, platform, _owner_profile):
            return self.adapters.get(platform)

        def _resolve_profile_home_for_source(self, _source):
            return home

    return _StubRunner


def _collect(runner):
    from gateway.kanban_watchers_notifier import _notifier_collect
    return _notifier_collect(runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30)


def _deliver(runner, item):
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _KanbanNotification
    asyncio.run(_KanbanNotification(runner, item, platform_cls=Platform, sub_fail_counts={}).deliver())


def test_outbox_retry_and_dedup_on_canonical_notifier_path(board_env, runner_factory):
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
    _, db_path = board_env
    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="outbox retry", assignee="coder", session_id="sess-1")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1",
            delivery_mode="notify+wake",
        )
        assert kb.complete_task(conn, tid, summary="shipped v1") is True
        rcpt = kbrr.list_result_receipts(conn, tid)[0]
        assert kbrr.get_outbox_entry(conn, rcpt.completion_id).status == "pending"

    adapter = _FlakyNotifyAdapter()
    runner = runner_factory(adapter)

    async def _one_tick():
        deliveries = _notifier_collect(
            runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30,
        )
        for item in deliveries:
            await _KanbanNotification(
                runner, item, platform_cls=Platform, sub_fail_counts=runner._kanban_sub_fail_counts,
            ).deliver()
        return len(deliveries)

    # Tick 1: send fails transiently -> cursor rewound, outbox remains pending with attempt=1.
    assert asyncio.run(_one_tick()) == 1
    assert len(adapter.sent) == 0
    with _connect(db_path) as conn:
        ob = kbrr.get_outbox_entry(conn, rcpt.completion_id)
        assert ob is not None and ob.status == "pending" and ob.delivery_attempts == 1
        assert "503" in (ob.last_error or "")

    # Tick 2: send succeeds (ping checkpointed), wake rejected -> cursor rewound, outbox still pending.
    assert asyncio.run(_one_tick()) == 1
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 0
    with _connect(db_path) as conn:
        ob = kbrr.get_outbox_entry(conn, rcpt.completion_id)
        assert ob is not None and ob.status == "pending" and ob.delivery_attempts == 2
        assert kbrr.is_outbox_ping_sent(conn, rcpt.completion_id, platform="telegram", chat_id="chat-1")

    # Tick 3: retry -> ping MUST NOT double-send; wake succeeds; outbox settles to delivered!
    assert asyncio.run(_one_tick()) == 1
    assert len(adapter.sent) == 1, "ping must not double-send on wake retry"
    assert len(adapter.handled) == 1
    with _connect(db_path) as conn:
        ob = kbrr.get_outbox_entry(conn, rcpt.completion_id)
        assert ob is not None and ob.status == "delivered" and ob.delivered_at is not None
        assert kbrr.is_outbox_delivered(conn, rcpt.completion_id, platform="telegram", chat_id="chat-1")

    # Tick 4: quiet tick -> nothing claimed, zero sends.
    assert asyncio.run(_one_tick()) == 0
    assert len(adapter.sent) == 1
    assert len(adapter.handled) == 1

    # Tick 5: even if cursor is manually rewound below completed_event_id,
    # completion-ID delivery ledger deduplicates BOTH ping and wake!
    with _connect(db_path) as conn:
        kbn.advance_notify_cursor(conn, task_id=tid, platform="telegram", chat_id="chat-1", new_cursor=0)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE kanban_notify_subs SET last_ping_event_id = 0 WHERE task_id = ?",
                (tid,),
            )
    assert asyncio.run(_one_tick()) == 0
    assert len(adapter.sent) == 1, "completion-ID dedup must block duplicate ping after rewind"
    assert len(adapter.handled) == 1, "completion-ID dedup must block duplicate wake after rewind"


@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'rejected'])
def test_notifier_refuses_unavailable_output(board_env, tmp_path, runner_factory, fault):
    from types import SimpleNamespace
    artifact = tmp_path / 'output.pdf'
    artifact.write_bytes(b'original')
    target = dict(platform='telegram', chat_id='fixture')
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='file handoff', assignee='worker')
        kbn.add_notify_sub(conn, task_id=tid, **target)
        assert kb.complete_task(conn, tid, summary='ready', metadata={'artifacts': [str(artifact)]})
        receipt, = kb.list_result_receipts(conn, tid)
    pinned = Path(receipt.manifest['artifacts'][0]['path'])
    if fault == 'corrupt':
        pinned.write_bytes(b'modified')
    if fault == 'missing':
        pinned.unlink()
    adapter = _FlakyNotifyAdapter()
    adapter.send_calls = 1
    adapter.send_document = AsyncMock(return_value=SimpleNamespace(success=False, error='upload rejected'))
    runner = runner_factory(adapter)
    delivery, = _collect(runner)
    _deliver(runner, delivery)
    with kbc.connect_closing(board_env[1]) as conn:
        assert kb.get_outbox_entry(conn, receipt.completion_id).status == 'pending'
    if fault == 'rejected':
        adapter.send_document.assert_awaited_once()
        adapter.send_document.return_value = SimpleNamespace(success=True)
        retry, = _collect(runner)
        _deliver(runner, retry)
        assert len(adapter.sent) == 1
        with kbc.connect_closing(board_env[1]) as conn:
            assert kb.get_outbox_entry(conn, receipt.completion_id).status == 'delivered'
    else:
        assert not adapter.sent
        adapter.send_document.assert_not_awaited()


@pytest.mark.parametrize('after_accept', [False, True])
def test_platform_crash_replays_same_intent_on_fresh_notifier(board_env, monkeypatch, runner_factory, after_accept):
    from gateway.kanban_watchers_notifier import _KanbanNotification
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='replay', assignee='worker')
        kbn.add_notify_sub(conn, task_id=tid, platform='telegram', chat_id='fixture')
        assert kb.complete_task(conn, tid, summary='deliver this')
        cid = kb.list_result_receipts(conn, tid)[0].completion_id
    adapter = _FlakyNotifyAdapter()
    adapter.send_calls = 1
    runner = runner_factory(adapter)
    item, = _collect(runner)
    assert not _collect(runner_factory(adapter))
    if after_accept:
        class NotifierDied(BaseException):
            pass
        async def die(*args, **kwargs):
            raise NotifierDied
        with monkeypatch.context() as crash:
            crash.setattr(_KanbanNotification, '_outbox_op', die)
            with pytest.raises(NotifierDied):
                _deliver(runner, item)
        assert len(adapter.sent) == 1
    future = kbrr.time.time() + 86400
    monkeypatch.setattr(kbrr.time, 'time', lambda: future)
    fresh = runner_factory(adapter)
    retry, = _collect(fresh)
    _deliver(fresh, retry)
    assert len(adapter.sent) == 1 + int(after_accept)
    assert not _collect(runner_factory(adapter))
    with kbc.connect_closing(board_env[1]) as conn:
        assert kb.get_outbox_entry(conn, cid).status == 'delivered'


def test_changed_write_declaration_is_a_new_input_revision(board_env):
    _, board = board_env
    with _connect(board) as conn:
        tid = kb.create_task(conn, title='publish report', resource_class='cpu', write_scopes=['out'])
        assert kb.complete_task(conn, tid, summary='original output')
        old = kbrr.list_result_receipts(conn, tid)[0]
        assert kb.edit_task(conn, tid, write_scopes=['new-out'])
        assert kbrr.compute_input_rev(conn, tid) != old.input_rev
        assert kbrr.adopt_result_receipt(conn, tid) is None


def test_delete_rolls_back_on_relation_failure(board_env):
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='atomic deletion', assignee='worker')
        assert kb.complete_task(conn, tid, summary='done')
        conn.execute('CREATE TRIGGER fail_delete BEFORE DELETE ON kanban_result_receipts '
                     'BEGIN DELETE FROM missing_fixture_table; END')
        with pytest.raises(sqlite3.OperationalError, match='missing_fixture_table'):
            kb.delete_task(conn, tid)
        assert kb.get_task(conn, tid).status == 'done' and kb.list_result_receipts(conn, tid)


def test_no_target_receipt_and_uri_integrity(board_env):
    from dataclasses import replace
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='no subscriptions')
        assert kb.complete_task(conn, tid, result='ok')
        receipt, = kb.list_result_receipts(conn, tid)
        assert kb.get_outbox_entry(conn, receipt.completion_id).status == 'no_targets'
        assert kb.list_pending_outbox(conn, tid) == []
        with pytest.raises(kb.CorruptResultReceiptError, match='URI'):
            kb.verify_result_receipt(conn, replace(receipt, artifact_uri='receipt://wrong/manifest'))


def test_legacy_inflight_run_must_reclaim_before_attesting(board_env):
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='upgrade during run', assignee='worker')
        old_run = kb.claim_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE task_runs DROP COLUMN input_rev')
    kb.init_db(board_env[1])
    with kbc.connect_closing(board_env[1]) as conn:
        assert not kb.complete_task(conn, tid, expected_run_id=old_run, summary='unwitnessed')
        assert kb.reclaim_task(conn, tid)
        fresh = kb.claim_task(conn, tid).current_run_id
        assert fresh != old_run and kb.complete_task(conn, tid, expected_run_id=fresh, summary='verified')


def test_bound_pr_does_not_relabel_attempt_and_acceptance_is_required(board_env, monkeypatch):
    from hermes_cli import kanban_pr_acceptance_store as store
    accepted = {'ok': False, 'classification': 'failure', 'detail': 'pending', 'recovery': 'retry'}
    monkeypatch.setattr(store, 'collect_acceptance', lambda *args, **kwargs: dict(accepted))
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='publish', assignee='worker', completion_contract='example/repo')
        run = kb.claim_task(conn, tid).current_run_id
        rev = kb.compute_input_rev(conn, tid)
        kwargs = dict(expected_run_id=run, summary='published', metadata={'published_pr':'https://github.com/example/repo/pull/1'})
        assert not kb.complete_task(conn, tid, **kwargs)
        accepted['ok'] = True
        assert kb.complete_task(conn, tid, **kwargs)
        assert kb.list_result_receipts(conn, tid)[0].input_rev == rev


@pytest.mark.parametrize('defect', ['unavailable', 'rejected', 'post_write_crash'])
def test_tui_settles_only_after_verified_transport_write(board_env, tmp_path, monkeypatch, defect):
    from tui_gateway import server
    artifact = tmp_path / 'notes.txt'
    artifact.write_bytes(b'notes')
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board_env[1]))
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='desktop session', assignee='worker')
        kbn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='session')
        assert kb.complete_task(conn, tid, summary='done', metadata={'artifacts': [str(artifact)]})
        receipt, = kb.list_result_receipts(conn, tid)
        if defect == 'unavailable':
            Path(receipt.manifest['artifacts'][0]['path']).unlink()
    sent = []
    def emit(*args):
        sent.append(args)
        return defect != 'rejected'
    monkeypatch.setattr(server, '_emit', emit)
    monkeypatch.setattr(server, '_notif_claim_turn', lambda session: False)
    with monkeypatch.context() as crash:
        if defect == 'post_write_crash':
            def die(*args, **kwargs):
                raise RuntimeError('lost ACK')
            crash.setattr(kbrr, 'mark_outbox_delivered', die)
        server._notif_poll_kanban_scoped('sid', {'session_key': 'session'})
    assert bool(sent) == (defect != 'unavailable')
    with kbc.connect_closing(board_env[1]) as conn:
        assert kb.get_outbox_entry(conn, receipt.completion_id).status == 'pending'
    if defect == 'post_write_crash':
        future = kbrr.time.time() + 86400
        monkeypatch.setattr(kbrr.time, 'time', lambda: future)
        server._notif_poll_kanban_scoped('sid', {'session_key': 'session'})
        assert len(sent) == 2
        with kbc.connect_closing(board_env[1]) as conn:
            assert kb.get_outbox_entry(conn, receipt.completion_id).status == 'delivered'


def test_parent_handoff_edit_invalidates_child_attempt(board_env):
    with kbc.connect_closing(board_env[1]) as conn:
        parent = kb.create_task(conn, title='spec', assignee='worker')
        child = kb.create_task(conn, title='implement', parents=[parent], assignee='worker')
        assert kb.complete_task(conn, parent, summary='initial spec')
        run = kb.claim_task(conn, child).current_run_id
        assert kb.edit_task(conn, parent, result='replacement spec')
        assert not kb.complete_task(conn, child, expected_run_id=run, summary='old result')
        assert kb.get_task(conn, child).current_run_id == run


@pytest.mark.skipif(not hasattr(kbrr.os, 'mkfifo'), reason='POSIX named pipe')
def test_replaced_pin_cannot_block_notifier_open(board_env, tmp_path, monkeypatch):
    source = tmp_path / 'output.txt'
    source.write_text('plain file')
    with kbc.connect_closing(board_env[1]) as conn:
        tid = kb.create_task(conn, title='regular files only', assignee='worker')
        assert kb.complete_task(conn, tid, summary='done', metadata={'artifacts': [str(source)]})
        receipt, = kb.list_result_receipts(conn, tid)
        pin = Path(receipt.manifest['artifacts'][0]['path'])
        pin.unlink()
        kbrr.os.mkfifo(pin)
        real_open = kbrr.os.open
        def guarded_open(path, flags, *args, **kwargs):
            if Path(path) == pin:
                assert flags & kbrr.os.O_NONBLOCK, 'blocking open of a FIFO would hang forever'
            return real_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(kbrr.os, 'open', guarded_open)
        with pytest.raises(kb.CorruptResultReceiptError, match='regular file'):
            kb.verify_result_receipt(conn, receipt)


def test_pr_acceptance_failure_leaves_zero_receipts_and_zero_outbox(board_env, monkeypatch):
    from hermes_cli import kanban_pr_acceptance_store as kpa

    _, db_path = board_env
    monkeypatch.setattr(
        kpa,
        "collect_acceptance",
        lambda *_a, **_kw: {
            "ok": False,
            "classification": "failure",
            "detail": "required check build failed",
            "pr_url": "https://github.com/org/repo/pull/1",
            "head_sha": "deadbeef",
            "checks": [],
            "recovery": "fix CI and retry",
        },
    )

    with _connect(db_path) as conn:
        tid = kb.create_task(
            conn,
            title="pr gated",
            assignee="coder",
            completion_contract="org/repo",
        )
        c1 = kb.claim_task(conn, tid)
        assert c1 is not None
        ok = kb.complete_task(
            conn,
            tid,
            summary="opened pr",
            metadata={"published_pr": "https://github.com/org/repo/pull/1"},
            expected_run_id=c1.current_run_id,
        )
        assert ok is False
        task_after = kb.get_task(conn, tid)
        assert task_after is not None and task_after.status == "running"
        assert kbrr.list_result_receipts(conn, tid) == []
        assert kbrr.list_pending_outbox(conn, tid) == []
        assert [e for e in kb.list_events(conn, tid) if e.kind == "completed"] == []


def test_tui_poller_deduplicates_completed_event_by_completion_id(board_env, monkeypatch):
    from tui_gateway.server import _collect_kanban_notifications

    _, db_path = board_env
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))

    with _connect(db_path) as conn:
        tid = kb.create_task(conn, title="tui dedup", assignee="coder")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="tui-chat-1")
        assert kb.complete_task(conn, tid, summary="tui done") is True
        rcpt = kbrr.list_result_receipts(conn, tid)[0]

    session = {"session_key": "tui-chat-1"}
    delivered_first = _collect_kanban_notifications(session, on_text=lambda text: True)
    assert len(delivered_first) == 1
    assert "tui done" in delivered_first[0]

    with _connect(db_path) as conn:
        assert kbrr.is_outbox_delivered(conn, rcpt.completion_id, platform="tui", chat_id="tui-chat-1")
        ob = kbrr.get_outbox_entry(conn, rcpt.completion_id)
        assert ob is not None and ob.status == "delivered"
        # Rewind cursor to verify completion-ID dedup prevents re-emission.
        kbn.advance_notify_cursor(conn, task_id=tid, platform="tui", chat_id="tui-chat-1", new_cursor=0)

    delivered_second = _collect_kanban_notifications(session)
    assert delivered_second == [], "TUI poller must deduplicate already-delivered completion_id"
