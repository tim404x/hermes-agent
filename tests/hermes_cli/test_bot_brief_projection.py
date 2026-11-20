"""A bot's task brief is never the human's own message (old rows, typed at read time).

A side window a bot opened for itself was seeded through ``bot_mode_dm.py --run-delivery --author bot:<self>``; rows
stored before the write side typed it hold the brief as a plain ``user`` row (default-profile session ``local-e3f3...``,
row 504398, 19 Nov 2026) whose own admission (``display_metadata.submission_id``) recorded the bot as the turn's
author. Both projections mark that first row ``bot_brief`` with that author; the human's later rows stay his. The tab
stamp is never the authorship: ``hermes sessions open-tab --by <bot>`` stamps a chat a person started.
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


def _seed(home, sid, *, by, first=BRIEF, author="bot:default"):
    """*author*: the turn author the first row's own admission recorded (None: a human's submission)."""
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession

    store = _store(home)
    try:
        store.sessions.create(NewSession(session_id=sid, source="cli"))
        turn = {"turn_author": {"id": author, "is_bot": True, "name": "hermes"}} if author else {}
        epoch = store.runtime.begin_epoch(instance_id="test")
        store.admissions.admit(epoch=epoch, principal_id="uid:1", session_id=sid, request_id=f"sub-{sid}",
                               payload={"cli_turn_v1": turn, "text": first})
        for i, (role, content) in enumerate((("user", first), ("assistant", TLDR), ("user", TIM),
                                              ("assistant", "On it."))):
            meta = {"submission_id": f"sub-{sid}"} if i == 0 else None
            store.messages.append(sid, NewMessage(role=role, content=content, display_metadata=meta))
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


@pytest.mark.parametrize("by, first, author", [
    (None, BRIEF, "bot:default"),                        # not a side window: a plain chat
    ("", BRIEF, None),                                   # a stamp with no author, a person's submission
    ("bot:default", "Message from 🤖 Kepler (@kepler): can you check X?", "bot:default"),  # an attributed DM
])
def test_other_windows_keep_their_user_rows(home, by, first, author):
    _seed(home, "w", by=by, first=first, author=author)
    users = _users(_rest_page("w"), "content")
    assert [kind for kind, _text, _author in users] == [None, None]


def test_brief_name_is_the_bots_short_display_name(home):
    (home / "profile.yaml").write_text('ui_meta:\n  hermes-bots:\n    title: "Neo | Chief of Staff"\n')
    _seed(home, "tab", by="bot:default")
    assert _users(_rest_page("tab"), "content")[0][2] == {"id": "bot:default", "name": "Neo"}


def test_a_bot_stamp_never_makes_a_humans_first_message_a_brief(home):
    """``open-tab --by <bot>`` on a chat a person started: the stamp names a bot, the first row's own admission does
    not, so the person's words stay theirs (codex + claude review P1, 19 Nov 2026)."""
    human = "Please compare the vendor quotes and call me before placing the order."
    _seed(home, "human-chat", by="bot:default", first=human, author=None)
    assert [kind for kind, _text, _author in _users(_rest_page("human-chat"), "content")] == [None, None]


def test_open_tab_by_a_bot_keeps_the_humans_first_message(home):
    """The documented call on a chat with no recorded admission at all (an old or imported chat)."""
    import argparse
    from hermes_cli.sessions_cmd import cmd_sessions
    from hermes_cli.subcommands.sessions import build_sessions_parser
    from persistence.conversation.messages import NewMessage
    from persistence.conversation.sessions import NewSession

    first = "build me a deal heatmap from the Airtable sales base, one row per buyer"
    store = _store(home)
    try:
        store.sessions.create(NewSession(session_id="local-abc", source="local",
                                         origin={"platform": "local", "chat_id": "local-abc", "user_id": "uid:1"}))
        for role, content in (("user", first), ("assistant", "On it."), ("user", "ok ship it")):
            store.messages.append("local-abc", NewMessage(role=role, content=content))
    finally:
        store.close()
    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions)
    assert cmd_sessions(parser.parse_args(["sessions", "open-tab", "local-abc", "--by", "neo"])) in (None, 0)
    users = _users(_rest_page("local-abc"), "content")
    assert users[0] == (None, first, None)
