"""The prompt.submit wire contract declares every field a parsed turn author may carry.

Live failure (24 Nov 2026, 23:16 UTC): a ``message_agent(target="me", new_tab=True)`` brief died with
``Session ... found but has no messages. Starting fresh.`` then ``Error: invalid_params``. The runner hands
the tab's author (``{"id": "bot:default", ..., "tab_depth": 1}``) to ``hermes chat`` in HERMES_TURN_AUTHOR,
the classic CLI forwards ``parse_turn_author``'s output as ``prompt.submit`` ``turn_author``, and the owner's
dispatcher (``canonical_param_problems``) refused the key ``turn_author.tab_depth`` (``CanonicalTurnAuthor``
is ``extra="forbid"`` and only knew id/name/is_bot/origin) before the handler ran. ``hops`` (bot-to-bot
chains) failed the same way.
"""
import pytest

from agent.turn_author import TURN_AUTHOR_CHAIN_FIELDS, parse_turn_author
from tui_gateway.contracts.registry import CANONICAL_METHODS, canonical_param_problems


def _submit(author):
    return {"session_id": "s", "text": "t", "submission_id": "x", "turn_author": author}


def test_every_turn_author_chain_field_passes_the_submit_contract():
    author = parse_turn_author({"id": "bot:default", "name": "hermes", "is_bot": True,
                                **{field: 2 for field in TURN_AUTHOR_CHAIN_FIELDS}})
    assert set(TURN_AUTHOR_CHAIN_FIELDS) <= set(author)
    assert canonical_param_problems(CANONICAL_METHODS["prompt.submit"], _submit(author)) == []


@pytest.mark.parametrize("raw", [
    # The exact author tools/bot_mode_dm.py::_open_self_tab stamps on a self tab (proc_e6b11c847cc0).
    '{"id":"bot:default","name":"hermes","is_bot":true,"tab_depth":1}',
    # A bot-to-bot reply further down a chain (agent/bot_chain.py adds hops > 1).
    '{"id":"bot:kepler","name":"Kepler","is_bot":true,"hops":3}',
])
def test_a_runtime_author_from_the_env_is_admissible(raw):
    contract = CANONICAL_METHODS["prompt.submit"]
    params = _submit(parse_turn_author(raw))
    assert canonical_param_problems(contract, params) == []
    validated = contract.params.model_validate(params).turn_author
    assert (validated.tab_depth, validated.hops) == (params["turn_author"].get("tab_depth"),
                                                     params["turn_author"].get("hops"))


def test_an_unknown_author_key_is_still_refused():
    params = _submit({"id": "bot:x", "name": "x", "is_bot": True, "rank": "admin"})
    assert canonical_param_problems(CANONICAL_METHODS["prompt.submit"], params) == ["turn_author.rank"]
