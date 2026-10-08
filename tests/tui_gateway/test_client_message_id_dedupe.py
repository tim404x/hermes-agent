"""One user send action runs at most once, whichever paths it takes (Tim, 8 Oct 2026).

The desktop sends a ``client_message_id`` per send action on ``prompt.submit``, ``session.redirect``
and ``session.steer`` (and on every queue drain of a local copy of that action). Two incidents in one
morning ran a single message as several turns:

- a queue re-keyed by a compaction rotation while its drain's submit was in flight was drained again,
  and two drains on two keys sent the same entry concurrently (seven busy accepts in eight seconds);
- a redirect the gateway queued but whose answer the client lost left a LOCAL copy that drained into
  ``prompt.submit`` two minutes later (two accepts, two turns, no rotation at all).

The client fixes stop the repeats it can see; this is the server safety net: an id that was ACCEPTED
(busy-queued, turn locked in, redirected, steered, or enqueued by a correction) answers ``duplicate``
with no enqueue and no turn. A REJECTED submit records nothing, so its retry still runs.
"""

from __future__ import annotations

import threading
import types

from tui_gateway import server


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


def _call(method, params, rid="r"):
    return server._methods[method](rid, params)


def _queued_texts(session):
    """Every queued text, with consecutive text-only follow-ups split back apart (the busy queue
    merges them into one envelope as ``a\n\nb``): a duplicate shows up as a repeated part."""
    head = session.get("queued_prompt")
    envelopes = ([head] if head else []) + list(session.get("queued_prompts") or [])
    return [part for e in envelopes for part in str(e["text"]).split("\n\n")]


def _with_session(session, fn):
    server._sessions["sid"] = session
    try:
        return fn()
    finally:
        server._sessions.pop("sid", None)


# ── prompt.submit busy path ────────────────────────────────────────────────


def test_repeat_of_an_accepted_busy_submit_is_a_duplicate_with_one_envelope(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session = _session(running=True)

    def run():
        first = _call("prompt.submit", {"session_id": "sid", "text": "and btw the report", "queued": True,
                                        "client_message_id": "queued-1-abc"})
        second = _call("prompt.submit", {"session_id": "sid", "text": "and btw the report", "queued": True,
                                         "client_message_id": "queued-1-abc"})
        return first, second

    first, second = _with_session(session, run)

    assert first["result"]["status"] == "queued"
    assert second["result"] == {"status": "duplicate", "running": True}
    assert _queued_texts(session) == ["and btw the report"]


def test_distinct_ids_still_queue_separately(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session = _session(running=True)

    def run():
        for n in (1, 2):
            resp = _call("prompt.submit", {"session_id": "sid", "text": f"message {n}", "queued": True,
                                           "client_message_id": f"queued-{n}"})
            assert resp["result"]["status"] == "queued"

    _with_session(session, run)

    assert _queued_texts(session) == ["message 1", "message 2"]


def test_missing_or_malformed_ids_keep_the_old_behaviour(monkeypatch):
    """Old clients send no id; a non-string id is ignored rather than trusted."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session = _session(running=True)

    def run():
        for n, cmid in enumerate((None, 7, "", "   ")):
            params = {"session_id": "sid", "text": f"legacy {n}", "queued": True}
            if cmid is not None:
                params["client_message_id"] = cmid
            assert _call("prompt.submit", params)["result"]["status"] == "queued"

    _with_session(session, run)

    assert _queued_texts(session) == ["legacy 0", "legacy 1", "legacy 2", "legacy 3"]


def test_a_rejected_submit_does_not_record_its_id(monkeypatch):
    """A busy truncation answers 4009 so the client retries; that retry must still run."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session = _session(running=True)

    def run():
        rejected = _call("prompt.submit", {"session_id": "sid", "text": "edit", "truncate_before_row_id": 3,
                                           "confirm_truncate": True, "client_message_id": "send-9"})
        retried = _call("prompt.submit", {"session_id": "sid", "text": "edit", "queued": True,
                                          "client_message_id": "send-9"})
        return rejected, retried

    rejected, retried = _with_session(session, run)

    assert rejected["error"]["code"] == 4009
    assert retried["result"]["status"] == "queued"
    assert _queued_texts(session) == ["edit"]


def test_a_repeat_while_the_first_is_still_being_decided_is_retryable_busy():
    """Two concurrent submits of one id: the second must neither run nor be told it was accepted
    (the first may still be rejected). 4009 is the busy code clients already retry on."""
    session = _session()

    assert server._claim_client_message_id(session, "send-1") == "new"
    assert server._claim_client_message_id(session, "send-1") == "pending"
    server._settle_client_message_id(session, "send-1", accepted=False)
    assert server._claim_client_message_id(session, "send-1") == "new"
    server._settle_client_message_id(session, "send-1", accepted=True)
    assert server._claim_client_message_id(session, "send-1") == "duplicate"


def test_the_accepted_id_record_is_bounded():
    session = _session()
    for n in range(server._CLIENT_MESSAGE_ID_LIMIT + 5):
        assert server._claim_client_message_id(session, f"id-{n}") == "new"
        server._settle_client_message_id(session, f"id-{n}", accepted=True)

    assert server._claim_client_message_id(session, "id-0") == "new"  # evicted, oldest first
    server._settle_client_message_id(session, "id-0", accepted=False)
    assert server._claim_client_message_id(session, f"id-{server._CLIENT_MESSAGE_ID_LIMIT + 4}") == "duplicate"


# ── prompt.submit idle path ────────────────────────────────────────────────


def test_repeat_of_a_turn_that_already_ran_starts_no_second_turn(monkeypatch):
    """The re-send of row 329203: the queued entry ran as a turn, the session went idle, and the same
    entry arrived again as a fresh idle submit."""
    session = _session()
    turns = []

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(server.threading, "Thread", InlineThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda rid, sid, session, text, *a: turns.append(text))

    def run():
        first = _call("prompt.submit", {"session_id": "sid", "text": "still have this",
                                        "client_message_id": "queued-77"})
        session["running"] = False  # the turn answered it and settled
        second = _call("prompt.submit", {"session_id": "sid", "text": "still have this",
                                         "client_message_id": "queued-77"})
        return first, second

    first, second = _with_session(session, run)

    assert first["result"]["status"] == "streaming"
    assert second["result"] == {"status": "duplicate", "running": False}
    assert turns == ["still have this"]


# ── two paths, one send action ─────────────────────────────────────────────


def test_redirect_queued_by_the_gateway_then_local_copy_drained_is_one_envelope(monkeypatch):
    """The 04:18 / 04:20 incident shape: the redirect landed in the server queue (build window) but
    the client kept a local copy of the same action, which it drained later via prompt.submit."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    session = _session(running=True)
    session["agent"] = None  # turn-build window: running, agent not wired yet

    def run():
        redirected = _call("session.redirect", {"session_id": "sid", "text": "no emoji in agent name tag",
                                                "client_message_id": "send-42"})
        drained = _call("prompt.submit", {"session_id": "sid", "text": "no emoji in agent name tag",
                                          "queued": True, "client_message_id": "send-42"})
        return redirected, drained

    redirected, drained = _with_session(session, run)

    assert redirected["result"]["status"] == "queued"
    assert drained["result"] == {"status": "duplicate", "running": True}
    assert _queued_texts(session) == ["no emoji in agent name tag"]


def test_redirect_repeating_an_accepted_submit_is_not_applied(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    applied = []
    agent = types.SimpleNamespace(_supports_active_turn_redirect=True,
                                  redirect=lambda text: applied.append(text) or True)
    session = _session(agent=agent, running=True)

    def run():
        _call("prompt.submit", {"session_id": "sid", "text": "use postgres", "queued": True,
                                "client_message_id": "send-5"})
        return _call("session.redirect", {"session_id": "sid", "text": "use postgres", "client_message_id": "send-5"})

    resp = _with_session(session, run)

    assert resp["result"] == {"status": "duplicate", "text": "use postgres", "running": True}
    assert applied == []
    assert _queued_texts(session) == ["use postgres"]


def test_an_applied_redirect_records_its_id_for_a_later_submit(monkeypatch):
    agent = types.SimpleNamespace(_supports_active_turn_redirect=True, redirect=lambda text: True)
    session = _session(agent=agent, running=True)

    def run():
        redirected = _call("session.redirect", {"session_id": "sid", "text": "change course",
                                                "client_message_id": "send-6"})
        submitted = _call("prompt.submit", {"session_id": "sid", "text": "change course", "queued": True,
                                            "client_message_id": "send-6"})
        return redirected, submitted

    redirected, submitted = _with_session(session, run)

    assert redirected["result"]["status"] == "redirected"
    assert submitted["result"]["status"] == "duplicate"
    assert _queued_texts(session) == []


def test_a_rejected_steer_does_not_record_its_id(monkeypatch):
    """An idle steer answers ``rejected`` and the client queues the words: that drain must run."""
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "queue")
    agent = types.SimpleNamespace(steer=lambda text: True)
    session = _session(agent=agent, running=False)

    def run():
        steered = _call("session.steer", {"session_id": "sid", "text": "setup note", "client_message_id": "send-8"})
        session["running"] = True
        submitted = _call("prompt.submit", {"session_id": "sid", "text": "setup note", "queued": True,
                                            "client_message_id": "send-8"})
        return steered, submitted

    steered, submitted = _with_session(session, run)

    assert steered["result"]["status"] == "rejected"
    assert submitted["result"]["status"] == "queued"
    assert _queued_texts(session) == ["setup note"]
