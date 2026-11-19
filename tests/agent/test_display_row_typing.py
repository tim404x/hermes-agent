"""Machine rows are typed for display at read time in BOTH history projections.

``session.resume`` (``tui_gateway/session_history.py::_history_to_messages``) and the REST page the desktop prefetches
(``hermes_cli/web_routers/sessions.py::_project_for_display``) must agree: a background-job title names the job, a
cron output delivered into a Bot Chat is never the user's own message, and an interrupt sentinel is never assistant
prose. The rows below are real stored rows (``/api/sessions/<id>/messages``, 19 Nov 2026), trimmed.
"""

import pytest

from agent.display_row_typing import type_display_row
from cron.scheduler_delivery import bot_chat_message, parse_bot_chat_frame
from hermes_cli.web_routers.sessions import _project_for_display
from tui_gateway.server import _history_to_messages

PROCESS_ROW = {
    "id": 504744, "session_id": "20261119_220431_6f05b2", "role": "user",
    "content": "[IMPORTANT: Background process proc_151f1eb6add3 completed normally (exit code 0).\n"
               "Command: W=/home/hermes/.hermes/work/asim-short-1119; for i in $(seq 1 90); do grep -q done $W/log; done\n"
               "Output:\nDONE\n]",
    "timestamp": 1795125943.9909973, "display_kind": "process_complete",
    "display_metadata": {"display_text": "Background Process Finished: W=/home/hermes/.hermes/work/asim-short-1119; "
                                         "for i in $(seq 1 90); do grep -q..."},
}

CRON_ROW = {
    "id": 74446, "session_id": "20261119_031855_926ce9", "role": "user",
    "content": '[Cronjob "🤖 Team report | Apex" output — scheduled job, not the user. Review it, act on anything '
               "that needs action, and summarize for the chat.]\n\n🎯 Apex Morning run is done. Your tasks and how "
               "they landed: team-reports/apex.md.",
    "timestamp": 1795094755.98743, "platform_message_id": "bot:0947a059e9ef", "display_kind": "internal_notification",
    "display_metadata": {"gateway_input_owner": "577e6f8c-1449-5182-b938-b4c4b6207708"},
}

INTERRUPT_ROW = {
    "id": 47510, "session_id": "20261116_041832_c1a526", "role": "assistant",
    "content": "Operation interrupted: waiting for model response (0.3s elapsed).",
    "timestamp": 1795058669.9406743, "display_kind": None, "display_metadata": {"interrupted": True},
}

TIM_ROW = {"id": 47511, "session_id": "20261116_041832_c1a526", "role": "user",
           "content": "show draft before sending", "timestamp": 1795058670.0, "display_kind": None,
           "display_metadata": None}


def _rest(row):
    return _project_for_display([dict(row)])[0]


def _resume(row):
    return _history_to_messages([{**row, "_row_id": row["id"]}])[0]


@pytest.mark.parametrize("project", [_rest, _resume], ids=["rest", "resume"])
def test_stored_process_title_names_the_job_in_both_projections(project):
    projected = project(PROCESS_ROW)
    assert projected["display_kind"] == "process_complete"
    assert projected["display_metadata"]["display_text"] == "Asim short finished"


def test_process_title_relabel_never_writes_back():
    row = {**PROCESS_ROW, "display_metadata": dict(PROCESS_ROW["display_metadata"])}
    _project_for_display([row])
    assert row["display_metadata"]["display_text"].startswith("Background Process Finished: W=/home/")


@pytest.mark.parametrize("project", [_rest, _resume], ids=["rest", "resume"])
def test_cron_output_is_a_typed_delivery_not_the_users_message(project):
    projected = project(CRON_ROW)
    assert projected["display_kind"] == "internal_notification"
    assert projected["display_metadata"]["cron_delivery"] == {"job_name": "🤖 Team report | Apex"}
    assert projected["display_metadata"]["display_text"] == "🤖 Team report | Apex"
    assert projected["display_metadata"]["gateway_input_owner"] == "577e6f8c-1449-5182-b938-b4c4b6207708"
    shown = projected.get("display_content", projected.get("text"))
    assert shown == "🎯 Apex Morning run is done. Your tasks and how they landed: team-reports/apex.md."


def test_rest_keeps_the_model_facing_cron_frame_as_content():
    assert _rest(CRON_ROW)["content"] == CRON_ROW["content"]


def test_untyped_cron_frame_row_is_typed_too():
    projected = _rest({**CRON_ROW, "display_kind": None, "display_metadata": None})
    assert projected["display_kind"] == "internal_notification"
    assert projected["display_metadata"] == {"cron_delivery": {"job_name": "🤖 Team report | Apex"},
                                             "display_text": "🤖 Team report | Apex"}


def test_a_user_quoting_the_cron_frame_mid_message_is_left_alone():
    row = {**TIM_ROW, "content": 'why does [Cronjob "x" output] show up?'}
    assert type_display_row(row) == row


def test_interrupt_sentinel_row_keeps_its_stop_mark_without_prose_rest():
    projected = _rest(INTERRUPT_ROW)
    assert projected["role"] == "assistant"
    assert projected["content"] == "" and projected["display_content"] == ""
    assert projected["display_metadata"] == {"interrupted": True}


def test_interrupt_sentinel_is_never_assistant_text_on_resume():
    texts = [m.get("text") for m in _history_to_messages([{**INTERRUPT_ROW, "_row_id": 47510},
                                                           {**TIM_ROW, "_row_id": 47511}])]
    assert not any("Operation interrupted" in (t or "") for t in texts)
    stopped = [m for m in _history_to_messages([{**INTERRUPT_ROW, "_row_id": 47510}]) if m["role"] == "assistant"]
    assert stopped and stopped[0]["text"] == "" and stopped[0]["display_metadata"] == {"interrupted": True}


@pytest.mark.parametrize("text", [
    "Operation interrupted: waiting for model response (12.4s elapsed).",
    "Operation interrupted: retrying API call after error (retry 2/3).",
    "Operation interrupted during retry (rate limit, attempt 1/3).",
])
def test_every_interrupt_sentinel_variant_is_blanked(text):
    assert type_display_row({**INTERRUPT_ROW, "content": text})["content"] == ""


def test_assistant_prose_mentioning_an_interrupt_is_kept():
    row = {**INTERRUPT_ROW, "content": "Operation interrupted: waiting for model response.\nHere is the fix."}
    assert type_display_row(row)["content"] == row["content"]


def test_bot_chat_frame_round_trips():
    framed = bot_chat_message({"name": "🏹 sales_day"}, "line one\n\nline two")
    assert parse_bot_chat_frame(framed) == ("🏹 sales_day", "line one\n\nline two")
    assert parse_bot_chat_frame("hello") is None
    assert parse_bot_chat_frame('[Cronjob "x" output — scheduled job, not the user.]') == ("x", "")
