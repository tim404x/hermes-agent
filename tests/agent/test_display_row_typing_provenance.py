"""Display typing needs machine provenance; prose and a person's words are never hidden or retyped.

Shared contract fixtures with the desktop twin (apps/desktop/src/lib/chat-messages/machine-rows.ts):
``tests/fixtures/interrupt_sentinel_cases.json`` and ``tests/fixtures/cron_frame_cases.json``.
"""

import json
from pathlib import Path

import pytest

from agent.display_row_typing import is_interrupt_sentinel, type_display_row
from hermes_cli.web_routers import sessions as rest
from tui_gateway.server import _history_to_messages

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_SENTINELS = json.loads((_FIXTURES / "interrupt_sentinel_cases.json").read_text(encoding="utf-8"))["cases"]
_CRON = json.loads((_FIXTURES / "cron_frame_cases.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _SENTINELS, ids=[c["text"][:60] for c in _SENTINELS])
def test_only_information_free_interrupt_sentinels_are_hidden(case):
    assert is_interrupt_sentinel(case["text"]) is case["sentinel"]
    projected = type_display_row({"role": "assistant", "content": case["text"]})
    shown = projected.get("display_content", projected["content"])
    assert (shown == "") is case["sentinel"], projected
    if not case["sentinel"]:
        assert shown == case["text"] and "interrupted" not in (projected.get("display_metadata") or {})


def test_a_real_assistant_error_survives_the_rest_projection():
    text = "Operation interrupted during upload: the file was not saved. Please retry."
    projected = rest._project_for_display([{"id": 1, "role": "assistant", "content": text}])[0]
    assert projected.get("display_content", projected["content"]) == text, projected


@pytest.mark.parametrize("case", _CRON, ids=[c["name"] for c in _CRON])
def test_a_user_row_is_a_cron_delivery_only_with_machine_provenance(case):
    row = {"id": 2, "role": "user", "content": case["text"], "display_kind": case["display_kind"]}
    projected = type_display_row(row)
    cron = (projected.get("display_metadata") or {}).get("cron_delivery")
    if case["cron"]:
        assert cron == {"job_name": case["job_name"]} and projected["display_content"] == case["output"]
    else:
        assert cron is None and projected.get("display_kind") == case["display_kind"]
        assert projected.get("display_content", projected["content"]) == case["text"]


def test_resume_keeps_the_cron_frame_in_the_user_rows_text():
    """The TUI resume paints ``text`` as the user's bubble: it keeps the frame naming the job; the output rides as
    ``display_content`` for clients that re-role the row (the desktop)."""
    case = next(c for c in _CRON if c["name"] == "typed producer row")
    out = _history_to_messages([
        {"role": "user", "content": case["text"], "display_kind": "internal_notification", "_row_id": 1},
        {"role": "assistant", "content": "Summary: all good.", "_row_id": 2},
    ])
    assert out[0]["role"] == "user" and out[0]["text"].startswith('[Cronjob "')
    assert out[0]["display_content"] == case["output"]
    assert out[0]["display_metadata"]["cron_delivery"] == {"job_name": case["job_name"]}
