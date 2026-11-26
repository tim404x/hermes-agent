"""Only host-typed machinery turns can settle a clean blank without a paid retry."""

import pytest

from agent import turn_empty_response as ter
from gateway import response_filters as gateway_filters
from tests.agent import test_notification_turn_empty_reply as notice

loop_agent = notice.loop_agent


@pytest.mark.parametrize("kind,requests", [("internal_notification", 1), (None, 2), ("background_result", 2)])
def test_only_machinery_turns_skip_paid_retries(loop_agent, kind, requests):
    model = loop_agent.client.chat.completions.create
    model.side_effect = [notice._empty_response(), notice._text_response("Board updated.")]
    result = loop_agent.run_conversation("[kanban] board updated", persist_user_display_kind=kind)
    assert model.call_count == requests
    assert result["final_response"] == ("" if requests == 1 else "Board updated.")


def test_quiet_exit_reason_and_row_match_the_gateway():
    assert ter.NOTIFICATION_NO_REPLY_EXIT == gateway_filters.NOTIFICATION_NO_REPLY_EXIT
    assert ter.MACHINERY_NO_REPLY_ROW in gateway_filters.LIVE_GATEWAY_SILENT_MARKERS
