"""The human title of a background-process completion row never carries the shell command.

Pinned by the shared cases in ``tests/fixtures/process_label_cases.json``; the desktop twin
(``apps/desktop/src/lib/process-title.ts``) reads the same file.
"""

import json
from pathlib import Path

import pytest

from tools.process_registry_notifications import (
    process_completion_display_text,
    process_job_label,
    relabel_legacy_process_title,
)

CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "process_label_cases.json").read_text())


@pytest.mark.parametrize("case", CASES["labels"], ids=lambda c: c["command"][:40] or "empty")
def test_job_label_from_command(case):
    assert process_job_label(case["command"]) == case["label"]


@pytest.mark.parametrize("case", CASES["titles"], ids=lambda c: c["title"])
def test_completion_title_names_the_job_not_the_command(case):
    assert process_completion_display_text(case["events"]) == case["title"]


@pytest.mark.parametrize("case", CASES["legacy"], ids=lambda c: c["display_text"][:40])
def test_legacy_stored_title_is_relabelled(case):
    assert relabel_legacy_process_title(case["display_text"]) == case["title"]


def test_lost_completion_reads_lost():
    evt = {"command": "W=/home/hermes/.hermes/work/asim-short-1119; sleep 5", "completion_reason": "lost", "exit_code": None}
    assert process_completion_display_text([evt]) == "Asim short lost"


@pytest.mark.parametrize("case", CASES["titles"], ids=lambda c: c["title"])
def test_title_is_a_fixed_point_of_the_legacy_relabel(case):
    title = process_completion_display_text(case["events"])
    assert relabel_legacy_process_title(title) == title
