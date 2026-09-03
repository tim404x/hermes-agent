"""Fork carry "pin in project" (tim404x c5d207fc69 / 1f1dde8220) on the storage interface.

Ported from ``tests/hermes_state/test_hermes_state.py::TestSessionPinAndStaleArchive`` when upstream
removed the old session store (be3d6affc8): the same four behaviours, now through
``SessionRepository.set_project_pinned`` on every backend, plus the sidebar mutation the desktop's
PATCH /api/sessions/{id} {"project_pinned": bool} rides (``Sidebar(project_pinned=...)``).
"""

from __future__ import annotations

import time

from persistence import SessionId
from persistence.authority.runtime import Sidebar, parse_mutation
from persistence.conversation.sessions import END_REASON_COMPRESSION, NewSession

PRINCIPAL = "principal-a"


def _new(store, session_id: str, parent: str | None = None) -> None:
    store.sessions.create(NewSession(session_id=SessionId(session_id), source="cli",
                                     parent_session_id=None if parent is None else SessionId(parent)))


def _stamp(store, session_id: str):
    return store.sessions.get(SessionId(session_id)).project_pinned_at


def test_set_project_pinned_stamps_time_and_clears(backend, scope_a):
    store = backend.open(scope_a)
    _new(store, "pp-s1")
    before = time.time()
    assert store.sessions.set_project_pinned(SessionId("pp-s1"), True) is True
    stamp = _stamp(store, "pp-s1")
    assert isinstance(stamp, float) and before - 1 <= stamp <= time.time() + 1
    # CONTRACT: the stamp rides the session row the listings and REST rows serve.
    assert store.sessions.get(SessionId("pp-s1")).as_session_row()["project_pinned_at"] == stamp
    assert store.sessions.set_project_pinned(SessionId("pp-s1"), False) is True
    assert _stamp(store, "pp-s1") is None


def test_project_pin_is_not_the_keep_flag(backend, scope_a):
    # Orthogonal flags: pinning in a project must not set `pinned`, and vice versa, or the row
    # would jump to the global Pinned section.
    store = backend.open(scope_a)
    _new(store, "pp-s2")
    store.sessions.set_project_pinned(SessionId("pp-s2"), True)
    assert store.sessions.get(SessionId("pp-s2")).pinned is False
    store.sessions.set_pinned(SessionId("pp-s2"), True)
    store.sessions.set_pinned(SessionId("pp-s2"), False)
    assert _stamp(store, "pp-s2") is not None


def test_later_project_pin_gets_a_greater_stamp(backend, scope_a):
    # "Newest pin on top" is derived from this ordering, so it must be strictly monotonic.
    store = backend.open(scope_a)
    _new(store, "pp-a")
    _new(store, "pp-b")
    store.sessions.set_project_pinned(SessionId("pp-a"), True)
    time.sleep(0.01)
    store.sessions.set_project_pinned(SessionId("pp-b"), True)
    assert _stamp(store, "pp-b") > _stamp(store, "pp-a")


def test_project_pin_covers_the_compression_lineage(backend, scope_a):
    # Pinning the surfaced tip must stamp the compressed root too (and the reverse), so the pin
    # survives the id rotation auto-compression does.
    store = backend.open(scope_a)
    _new(store, "pp-root")
    store.sessions.end(SessionId("pp-root"), END_REASON_COMPRESSION)
    _new(store, "pp-tip", parent="pp-root")
    assert store.sessions.set_project_pinned(SessionId("pp-tip"), True) is True
    assert _stamp(store, "pp-root") is not None
    assert _stamp(store, "pp-root") == _stamp(store, "pp-tip")
    assert store.sessions.set_project_pinned(SessionId("pp-root"), False) is True
    assert _stamp(store, "pp-tip") is None


def test_missing_session_reports_false(backend, scope_a):
    store = backend.open(scope_a)
    assert store.sessions.set_project_pinned(SessionId("pp-missing"), True) is False


def test_sidebar_mutation_parses_and_writes_the_pin_time(backend, scope_a):
    # The desktop's "Pin in project" is PATCH /api/sessions/{id} {"project_pinned": true}, which the
    # dashboard turns into a sidebar mutation: it must parse (not invalid_params) and stamp the lineage.
    assert parse_mutation("sidebar", {"project_pinned": True}) == Sidebar(project_pinned=True)
    store = backend.open(scope_a)
    epoch = store.runtime.begin_epoch(instance_id="instance-a")
    _new(store, "pp-mroot")
    store.sessions.end(SessionId("pp-mroot"), END_REASON_COMPRESSION)
    _new(store, "pp-mtip", parent="pp-mroot")
    before = time.time()
    outcome = store.runtime.mutate(epoch, principal_id=PRINCIPAL, session_id="pp-mtip", request_id="req-pp-1",
                                   expected_revision=store.sessions.get("pp-mtip").runtime_revision,
                                   command=Sidebar(project_pinned=True), owner=None)
    assert outcome.result["project_pinned"] is True
    for sid in ("pp-mroot", "pp-mtip"):
        stamp = _stamp(store, sid)
        assert stamp is not None and before - 1 <= stamp <= time.time() + 1, sid
        # CONTRACT: the pin is an ordering stamp, never the keep flag.
        assert store.sessions.get(SessionId(sid)).pinned is False
    store.runtime.mutate(epoch, principal_id=PRINCIPAL, session_id="pp-mtip", request_id="req-pp-2",
                         expected_revision=store.sessions.get("pp-mtip").runtime_revision,
                         command=Sidebar(project_pinned=False), owner=None)
    assert _stamp(store, "pp-mroot") is None and _stamp(store, "pp-mtip") is None
