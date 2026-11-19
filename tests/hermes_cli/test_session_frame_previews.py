"""Harness frames never surface in a session or roster preview.

A mid-turn steer is stored wrapped in ``[OUT-OF-BAND USER MESSAGE …]``; background-process and delegation
completions are ``[IMPORTANT: …]`` / ``[ASYNC DELEGATION …]`` user rows. The desktop sidebar showed
``[OUT-OF-BAND USER MES…`` under a bot until the preview reads went through ``display_preview_text``.
These drive the real SQL + shaping path through the storage interface.
"""

import pytest

from agent.prompt_builder import steer_user_row
from persistence.conversation.sessions import SessionQuery, SessionWindow
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session
from tui_gateway.methods_profiles import _latest_message_preview

PROCESS_DONE = ("[IMPORTANT: Background process proc_1 completed normally (exit code 0).\n"
                "Command: bash x.sh\nOutput:\n[1, 2]\n]")
DELEGATION_DONE = "[ASYNC DELEGATION BATCH COMPLETE — deleg_1]\nA background fan-out unit has finished."


@pytest.fixture()
def db(tmp_path):
    session_db = open_persistence(profile_scope(home=tmp_path))
    yield session_db
    session_db.close()


def _listed(db):
    return {entry.as_list_row()["id"]: entry.as_list_row()
            for entry in db.sessions.list_sessions(SessionQuery(), SessionWindow(limit=10))}


def _seed(db, session_id, *messages):
    seed_session(db, session_id, "cli", model="m")
    seed_messages(db, session_id, *messages)


def test_session_preview_unwraps_a_leading_steer_row(db):
    steer = steer_user_row("put the pricing slide first")
    _seed(db, "s1", {"role": "user", "content": steer["content"], "display_kind": "steer"},
          {"role": "assistant", "content": "Done."})
    assert _listed(db)["s1"]["preview"] == "put the pricing slide first"


def test_session_preview_skips_frame_only_rows(db):
    _seed(db, "s1", {"role": "user", "content": PROCESS_DONE, "display_kind": "process_complete"},
          {"role": "user", "content": DELEGATION_DONE},
          {"role": "user", "content": "what did the batch send?"},
          {"role": "assistant", "content": "40 connects."})
    assert _listed(db)["s1"]["preview"] == "what did the batch send?"


def test_roster_preview_unwraps_the_newest_steer(db):
    steer = steer_user_row("move the call to 3pm")
    _seed(db, "s1", {"role": "user", "content": "draft the recap"}, {"role": "assistant", "content": "On it."},
          {"role": "user", "content": steer["content"], "display_kind": "steer"})
    assert _latest_message_preview(db, "s1") == "move the call to 3pm"


def test_roster_preview_falls_back_past_frame_only_rows(db):
    _seed(db, "s1", {"role": "user", "content": "run the batch"}, {"role": "assistant", "content": "Started it."},
          {"role": "user", "content": PROCESS_DONE, "display_kind": "process_complete"})
    assert _latest_message_preview(db, "s1") == "Started it."


def test_roster_preview_of_plain_text_is_unchanged(db):
    _seed(db, "s1", {"role": "user", "content": "hi"}, {"role": "assistant", "content": "  hello   there  "})
    assert _latest_message_preview(db, "s1") == "hello there"


def test_roster_preview_skips_a_silent_wake(db):
    """A background-process wake the bot answered with a bare silence marker painted ``NO_REPLY`` as
    the bot's sidebar line; the preview falls back to the newest row a person can read."""
    _seed(db, "s1", {"role": "user", "content": "build the heatmap"},
          {"role": "assistant", "content": "The heatmap is ready for you to publish."},
          {"role": "user", "content": PROCESS_DONE, "display_kind": "process_complete"},
          {"role": "assistant", "content": "NO_REPLY"})
    assert _latest_message_preview(db, "s1") == "The heatmap is ready for you to publish."


def test_roster_preview_keeps_a_reply_that_mentions_the_marker(db):
    _seed(db, "s1", {"role": "user", "content": "what did you answer?"},
          {"role": "assistant", "content": "I answered NO_REPLY on purpose."})
    assert _latest_message_preview(db, "s1") == "I answered NO_REPLY on purpose."
