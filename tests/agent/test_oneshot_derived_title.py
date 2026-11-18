"""One-shot runs get a free ``derived`` title from their prompt, never a model call (fork carry)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.title_generator import apply_oneshot_title, oneshot_title_line
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle

JOB = """You are Neo, running a job.

# Hide background job runs from the iOS session list (Neo job, 18 Nov 2026 ~2:20 PM PT)

Tim, verbatim: ...
"""


@pytest.mark.parametrize("prompt,expected", [
    (JOB, "Hide background job runs from the iOS session list"),
    ("Reply PONG", "Reply PONG"),
    ("\n\n## Not a top heading\nbody", "Not a top heading"),
    ("Outbox worker contract (v3)\nmore", "Outbox worker contract"),
    ("(only a parenthetical)", "(only a parenthetical)"),
])
def test_title_line_prefers_the_h1_and_drops_the_dateline(prompt, expected):
    assert oneshot_title_line(prompt) == expected


def test_oneshot_title_is_derived_and_repeats_take_a_suffix(tmp_path, close_storage_handles):
    db = storage_handle(tmp_path)
    prompt = "Outbox worker contract: drain the queue and draft replies (run 2026-11-18)\nsteps..."
    titles = []
    with patch("agent.title_generator.auto_title_session") as model_call, \
            patch("agent.title_generator.generate_title") as gen:
        for sid in ("s1", "s2", "s3"):
            seed_session(db, sid, "oneshot")
            titles.append(apply_oneshot_title(db, sid, prompt, persistence=db))
    model_call.assert_not_called()
    gen.assert_not_called()
    assert titles[0] and titles[0].startswith("Outbox worker contract")
    assert titles[1] == f"{titles[0]} #2" and titles[2] == f"{titles[0]} #3"
    assert [db.sessions.get_title(s) for s in ("s1", "s2", "s3")] == titles
    assert session_row(db, "s1")["title_source"] == "derived"


def test_oneshot_title_never_raises(tmp_path, close_storage_handles):
    db = storage_handle(tmp_path)
    seed_session(db, "s1", "oneshot")
    with patch("agent.title_generator._persist_session_title", side_effect=ValueError("taken")):
        assert apply_oneshot_title(db, "s1", "# Same title", persistence=db) is None
    assert apply_oneshot_title(db, "missing-session", "# Hi there", persistence=db) is None


def test_oneshot_title_respects_disabled_auto_titles(tmp_path, close_storage_handles):
    db = storage_handle(tmp_path)
    seed_session(db, "s1", "oneshot")
    with patch("agent.title_generator._auto_title_enabled", return_value=False):
        assert apply_oneshot_title(db, "s1", "# Off", persistence=db) is None
    assert db.sessions.get_title("s1") is None
