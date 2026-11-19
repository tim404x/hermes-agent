"""``display_preview_text``: the one projection every list row (session preview, roster last message, timeline) uses.

Cases live in ``tests/fixtures/preview_text_cases.json`` and are shared with the desktop twin
(``apps/desktop/src/lib/preview-text.test.ts``), so the server and the renderer cannot drift apart.
"""

import json
from pathlib import Path

import pytest

from agent.prompt_builder import STEER_MARKER_CLOSE, STEER_MARKER_OPEN, steer_user_row
from session_rules.preview_text import display_preview_text

_CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "preview_text_cases.json").read_text(encoding="utf-8"))
_CASES = _CASES["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_shared_preview_cases(case):
    assert display_preview_text(case["input"]) == case["expected"]


def test_shared_cases_use_the_live_steer_marker():
    # The fixture hard-codes the marker; a reworded marker must be re-pinned there, not silently pass.
    assert any(STEER_MARKER_OPEN in case["input"] and STEER_MARKER_CLOSE in case["input"] for case in _CASES)


def test_a_real_steer_row_previews_as_the_typed_words():
    assert display_preview_text(steer_user_row("put the pricing slide first")["content"]) == "put the pricing slide first"


def test_skill_scaffold_previews_as_the_typed_command(tmp_path, monkeypatch):
    from agent import skill_commands
    from tools import skills_tool

    skill_dir = tmp_path / "skills" / "work"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: work\ndescription: d\n---\n\n# work\n\nbody text\n")
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(skill_commands, "_skill_commands_by_key", {})
    skill_commands.scan_skill_commands()
    message = skill_commands.build_skill_invocation_message("/work", user_instruction="fix the title leak")
    assert display_preview_text(message) == "/work — fix the title leak"
