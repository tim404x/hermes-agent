"""Ack first on an image turn: the note is API-only, on every path.

A person's image turn asks the model for its ack line exactly like a text turn, but the note never
reaches the stored user row: the transcript, session search, exports and compaction read only what
the person sent. The row's ``api_content`` sidecar keeps the text projection of what the request
carried, so the next request replays that historical turn with the same bytes and the prompt-cache
prefix holds:

- Telegram native image (list content, plain-text persist override, ordinary flush): the next turn is
  rebuilt from the store and image replay re-attaches the photo.
- Desktop image (the submit-time row the turn adopts, list persist override): the warm agent keeps its
  live history after the finalizer applied the clean override.
"""

from __future__ import annotations

import re
import types

import pytest

from agent import image_replay
from agent.ack_first import ACK_FIRST_NOTE, stage_ack_first
from agent.codex_responses_adapter import _summarize_user_message_for_log
from agent.context_compressor import _DB_PERSISTED_MARKER
from agent.session_persistence import SessionPersistenceMixin, _db_flush_row
from agent.turn_context import build_api_messages
from persistence_assembly import open_persistence, profile_scope
from session_rules.turns import _durable_content
from tests.agent.test_turn_context import _build, _FakeAgent, _stub_runtime_main
from tests.fixtures.storage_read import conversation, transcript_rows
from tests.fixtures.storage_seed import seed_messages, seed_session

IMAGE = "data:image/png;base64,iVBORw0KGgo="
# A phrase of the note as session search would be asked for it.
NOTE_PHRASE = '"' + " ".join(re.findall(r"[A-Za-z]+", ACK_FIRST_NOTE)[:3]) + '"'


def _agent(store=None, session_id="s1"):
    agent = _FakeAgent()
    agent.session_id = session_id
    agent.interim_assistant_callback = lambda *_a, **_k: None
    agent._copy_reasoning_content_for_api = lambda *_a: None
    agent._should_sanitize_tool_calls = lambda: False
    agent.ephemeral_system_prompt = ""
    if store is not None:
        agent.persistence = store
        agent._session_db = types.SimpleNamespace()
    return agent


def _turn(agent, user_message, **kwargs):
    return _build(agent, user_message=user_message, summarize_user_message_for_log=_summarize_user_message_for_log,
                  **kwargs)


def _user_wire(agent, messages, idx, plugin_user_context=""):
    """The first user message of the request ``build_api_messages`` sends for ``messages``."""
    wire, _ = build_api_messages(
        agent, messages, current_turn_user_idx=idx, ext_prefetch_cache="",
        plugin_user_context=plugin_user_context, moa_config=None, active_system_prompt="SYSTEM")
    return next(m["content"] for m in wire if m.get("role") == "user")


def _notes_in(content) -> int:
    return str(content).count(ACK_FIRST_NOTE)


def test_telegram_native_image_turn_stores_no_note_and_replays_the_sent_bytes(tmp_path, monkeypatch):
    photo = str(tmp_path / "native-inputs" / "ab12" / "photo.jpg")
    parts = [{"type": "text", "text": f"what is this part?\n\n[Image attached at: {photo}]"},
             {"type": "image_url", "image_url": {"url": IMAGE}}]
    agent = _agent(session_id="tg")
    stage_ack_first(agent, parts, person_turn=True)
    ctx = _turn(agent, [dict(p) for p in parts], persist_user_message="what is this part?")
    sent = _user_wire(agent, ctx.messages, ctx.current_turn_user_idx, ctx.plugin_user_context)
    assert _notes_in(sent) == 1

    row = _db_flush_row(agent, ctx.messages[ctx.current_turn_user_idx], True)
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "tg", "telegram")
        seed_messages(db, "tg", row)
        stored = transcript_rows(db, "tg")[0]["content"]
        assert _notes_in(stored) == 0 and "what is this part?" in stored
        assert db.search.search_messages(NOTE_PHRASE) == ()
        history = conversation(db, "tg")

    # The next turn is rebuilt from the store; the photo is retained, so image replay re-attaches it.
    monkeypatch.setattr(image_replay, "_turn_replay_window", lambda _agent: 3)
    monkeypatch.setattr(image_replay, "_retained_data_url", lambda ref: IMAGE if ref == photo else None)
    replayed = _user_wire(agent, [*history, {"role": "assistant", "content": "A brake caliper."},
                                  {"role": "user", "content": "and a US supplier?"}], 2)
    assert replayed == sent


def test_desktop_adopted_image_row_stores_no_note_and_the_warm_turn_keeps_its_bytes(tmp_path):
    clean = [{"type": "text", "text": "what is this part?"}, {"type": "image_url", "image_url": {"url": IMAGE}}]
    with open_persistence(profile_scope(home=tmp_path)) as db:
        agent = _agent(db, session_id="desk")
        seed_session(db, "desk", "tui")
        # prompt.submit wrote the row at send time; the turn adopts it (tui_gateway _adopt_submit_user_row).
        row_id = seed_messages(db, "desk", {"role": "user", "content": _durable_content(clean)})[0]
        agent._pending_cli_user_message = {"role": "user", "content": [dict(p) for p in clean], "_row_id": row_id,
                                           _DB_PERSISTED_MARKER: True}
        stage_ack_first(agent, clean, person_turn=True)
        ctx = _turn(agent, [dict(p) for p in clean], persist_user_message=[dict(p) for p in clean])
        idx = ctx.current_turn_user_idx
        sent = _user_wire(agent, ctx.messages, idx, ctx.plugin_user_context)
        assert _notes_in(sent) == 1

        stored = transcript_rows(db, "desk")[0]
        assert _notes_in(stored["content"]) == 0
        assert db.search.search_messages(NOTE_PHRASE) == ()
        # The adopted row's sidecar holds what the request sent (a cold resume replays it).
        assert conversation(db, "desk")[0].get("api_content") == _durable_content(sent)

    # Turn end: the finalizer applies the clean list override to live history (warm desktop agent).
    SessionPersistenceMixin._apply_persist_user_message_override(agent, ctx.messages)
    assert _notes_in(ctx.messages[idx]["content"]) == 0
    replayed = _user_wire(agent, [*ctx.messages, {"role": "assistant", "content": "A brake caliper."},
                                  {"role": "user", "content": "and a US supplier?"}], idx + 2)
    assert replayed == sent


@pytest.mark.parametrize("trailing_text", [False, True])
def test_the_note_rides_the_wire_as_the_text_a_rebuilt_row_produces(trailing_text):
    # Caption after the image (desktop paste order) or before it (gateway native parts): either way the
    # request's parts are exactly what replaying the stored text projection rebuilds.
    caption = {"type": "text", "text": "what is this part?"}
    image = {"type": "image_url", "image_url": {"url": IMAGE}}
    parts = [image, caption] if trailing_text else [caption, image]
    agent = _agent()
    stage_ack_first(agent, parts, person_turn=True)
    ctx = _turn(agent, [dict(p) for p in parts])
    sent = _user_wire(agent, ctx.messages, ctx.current_turn_user_idx, ctx.plugin_user_context)
    assert _notes_in(sent) == 1
    assert sum(1 for p in sent if p.get("type") == "text") == (1 if trailing_text else 2)
    assert _notes_in(ctx.messages[ctx.current_turn_user_idx]["content"]) == 0


def test_a_text_turn_keeps_the_note_in_the_api_sidecar_only(tmp_path):
    agent = _agent(session_id="txt")
    stage_ack_first(agent, "pull Figure's October hours and chart them", person_turn=True)
    ctx = _turn(agent, "pull Figure's October hours and chart them",
                 persist_user_message="pull Figure's October hours and chart them")
    row = _db_flush_row(agent, ctx.messages[ctx.current_turn_user_idx], True)
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "txt", "telegram")
        seed_messages(db, "txt", row)
        assert _notes_in(transcript_rows(db, "txt")[0]["content"]) == 0
        assert db.search.search_messages(NOTE_PHRASE) == ()
        assert _notes_in(conversation(db, "txt")[0].get("api_content")) == 1
