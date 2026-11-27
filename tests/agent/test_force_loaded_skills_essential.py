import ast
from pathlib import Path

import pytest

from agent import skill_utils
from hermes_constants import get_hermes_home
from tools import skill_usage


ROOT = Path(__file__).resolve().parents[2]


def test_force_loaded_skills_are_essential_and_bundled():
    bundled = {skill_usage.read_skill_name(md, "") for md in
               skill_utils.iter_skill_index_files(ROOT / "skills", "SKILL.md")}
    assert skill_utils.FORCE_LOADED_SKILLS <= skill_utils.ESSENTIAL_SKILLS & bundled


@pytest.mark.parametrize(("module", "required"), [
    ("hermes_cli/kanban_db_dispatch.py", {"REVIEW_LANE_SKILL"}),
    ("gateway/session_kanban.py", {"REVIEW_LANE_SKILL"}),
    ("hermes_cli/kanban_swarm.py", {"SWARM_VERIFIER_SKILL", "SWARM_SYNTHESIZER_SKILL"}),
])
def test_kanban_force_loads_use_registered_constants(module, required):
    names = set()
    for node in ast.walk(ast.parse((ROOT / module).read_text(encoding="utf-8"))):
        keyword = isinstance(node, ast.keyword) and node.arg == "skills"
        assigned = isinstance(node, ast.Assign) and any(ast.unparse(target).split(".")[-1] == "skills" for target in node.targets)
        if not (keyword or assigned):
            continue
        names.update(ast.unparse(item) for seq in ast.walk(node.value) if isinstance(seq, ast.List)
                     for item in seq.elts if not isinstance(item, ast.Starred))
    assert required <= names
    for name in names:
        assert getattr(skill_utils, name, None) in skill_utils.FORCE_LOADED_SKILLS, name


@pytest.mark.parametrize("name", sorted(skill_utils.ESSENTIAL_SKILLS))
def test_curator_never_archives_an_essential(name):
    home = get_hermes_home()
    (home / "config.yaml").write_text("curator:\n  prune_builtins: true\n")
    skills = home / "skills"
    (skills / ".bundled_manifest").write_text(f"{name}:hash\n")
    skill = skills / name / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(f"---\nname: {name}\n---\nbody\n")
    assert skill_usage._prune_builtins_enabled()
    assert skill_usage.is_protected_builtin(name)
    ok, message = skill_usage.archive_skill(name)
    assert not ok and "protected" in message and skill.is_file()
