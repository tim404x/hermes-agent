"""A bot's task brief is never the human's own message (old rows, typed at read time).

A side window a bot opened for itself (``origin_json.side_window.by = "bot:<profile>"``) was seeded through
``bot_mode_dm.py --run-delivery --author bot:<self>``; rows stored before the write side typed it hold the brief as a
plain ``user`` row (default-profile session ``local-e3f3...``, row 504398, 19 Nov 2026). Both projections mark that
first row ``bot_brief`` with its author; the human's later rows in the tab stay his.
"""

import asyncio
from pathlib import Path

import pytest

from agent.display_row_typing import find_bot_brief
from persistence.conversation.messages import ConversationRequest
from gateway.session_controls import display_projection
from hermes_cli.web_routers import sessions as rest
from tui_gateway.server import _history_to_messages

BRIEF = ("Tim asked for a dedicated working tab for this task (Thu 19 Nov, 2 PM PT). This tab IS that tab.\n"
         "Your reply in this turn is what Tim sees first: post this TLDR first.")
TLDR = "TLDR: hardware tab is live."
TIM = "ok, now check the vendor quote"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _store(home):
    from persistence_assembly import open_persistence, profile_scope
    return open_persistence(profile_scope(home=home), eager=True)


def _seed(home, sid, *, by, first=BRIEF):
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession

    store = _store(home)
    try:
        store.sessions.create(NewSession(session_id=sid, source="cli"))
        for role, content in (("user", first), ("assistant", TLDR), ("user", TIM), ("assistant", "On it.")):
            store.messages.append(sid, NewMessage(role=role, content=content))
        if by is not None:
            assert store.sessions.mark_side_window(sid, by=by, at=1000.0)
    finally:
        store.close()


def _rest_page(sid):
    return asyncio.run(rest.get_session_messages(sid, profile=None, limit=None, offset=0, order=None,
                                                 include_compacted=False, inline_images=True))["messages"]


def _users(messages, text_key):
    return [(m.get("display_kind"), m.get(text_key), (m.get("display_metadata") or {}).get("author"))
            for m in messages if m["role"] == "user"]


def test_rest_page_marks_a_self_seeded_tabs_brief_as_the_bots(home):
    _seed(home, "tab", by="bot:default")
    users = _users(_rest_page("tab"), "content")
    assert users[0] == ("bot_brief", BRIEF, {"id": "bot:default", "name": "bot:default"})
    assert users[1] == (None, TIM, None)


def test_resume_projection_marks_the_same_brief(home):
    _seed(home, "tab", by="bot:default")
    store = _store(home)
    try:
        brief = find_bot_brief(store, "tab")
        rows = [m.as_openai_message() for m in store.messages.read_conversation(
            "tab", ConversationRequest(include_row_ids=True))]
    finally:
        store.close()
    for project in (lambda: _history_to_messages(rows, bot_brief=brief),
                    lambda: display_projection([{**r, "row_id": r["_row_id"]} for r in rows], None, bot_brief=brief)):
        users = _users(project(), "text")
        assert users[0] == ("bot_brief", BRIEF, {"id": "bot:default", "name": "bot:default"})
        assert users[1] == (None, TIM, None)


@pytest.mark.parametrize("by, first", [
    (None, BRIEF),                                       # not a side window: a plain chat
    ("", BRIEF),                                         # a stamp with no author
    ("bot:default", "Message from 🤖 Kepler (@kepler): can you check X?"),  # an attributed DM
])
def test_other_windows_keep_their_user_rows(home, by, first):
    _seed(home, "w", by=by, first=first)
    users = _users(_rest_page("w"), "content")
    assert [kind for kind, _text, _author in users] == [None, None]


def test_brief_name_is_the_bots_short_display_name(home):
    (home / "profile.yaml").write_text('ui_meta:\n  hermes-bots:\n    title: "Neo | Chief of Staff"\n')
    _seed(home, "tab", by="bot:default")
    assert _users(_rest_page("tab"), "content")[0][2] == {"id": "bot:default", "name": "Neo"}
