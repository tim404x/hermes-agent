"""A one-shot session (``hermes chat -q`` / ``-z`` / ``--oneshot``) starts no auto-title call.

It still gets a free ``derived`` title from its prompt (``apply_oneshot_title``, no model call) so a
client that lists it shows a name instead of an untitled row (fork carry).

Nothing shows a one-shot's title (those sessions are hidden from every picker), and on a process
provider such as the Claude subscription DirectSDK plugin the title call is a second paid CLI spawn.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _Agent:
    def __init__(self):
        self.platform = "cli"
        self.session_id = "sess-1"
        self.model, self.provider, self.base_url = "m", "openrouter", "https://openrouter.ai/api/v1"
        self.api_key, self.api_mode = "k", "chat_completions"
        self._session_db = MagicMock()
        self._session_db_created = True


@pytest.mark.parametrize("oneshot", [True, False])
def test_only_an_interactive_first_turn_starts_the_title_call(oneshot, monkeypatch):
    from agent import turn_context

    if oneshot:
        monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    else:
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    with patch("agent.title_generator.maybe_auto_title") as titler, \
            patch("agent.title_generator.apply_oneshot_title") as free_title:
        turn_context._maybe_title_session_at_turn_start(_Agent(), [{"role": "user", "content": "Fix the login"}])
    assert titler.called is not oneshot
    assert free_title.called is oneshot
    if oneshot:
        assert free_title.call_args.args[1:] == ("sess-1", "Fix the login")
