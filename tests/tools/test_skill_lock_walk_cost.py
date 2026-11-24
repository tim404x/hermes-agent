"""The skill-lock guard finds locked skills without walking every skill's support tree (t_7e4c9e3c).

The guard runs on every terminal, write_file and patch call of every in-process agent. Its full
``rglob('SKILL.md')`` also walked each skill's references/scripts/assets/templates and VCS/dependency
dirs, whose SKILL.md files the exclusion filter then drops anyway: ~19k entries and ~80 ms of
GIL-held Python per call on Tim's profile. On 24 Nov 2026 it was the largest GIL consumer in the
gateway owner (20% of GIL samples), and the owner loop's synchronous reads starved behind it into
>10 s stalls (``ws write slow (loop stalled >10.0s)``) that dropped workers' sockets.
"""
import os

import pytest


def _skill(root, rel, name=None):
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name or path.name}\ndescription: x\n---\nbody\n")
    return path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    skills = home / "skills"
    locked = _skill(skills, "ops/locked-skill")
    _skill(skills, "ops/free-skill")
    _skill(skills, "solo")
    _skill(skills, "ops/locked-skill/nested-skill")  # a skill inside a skill (not a support dir) counts
    # Support trees beside a SKILL.md and VCS/dependency dirs: big, and every SKILL.md in them is excluded.
    for support in ("references", "scripts", "assets", "templates"):
        for index in range(30):
            (locked / support / f"d{index}" / "deep").mkdir(parents=True)
        _skill(locked / support, "decoy-skill")
    for excluded in (".git", "node_modules", ".archive", "__pycache__"):
        _skill(skills / "ops" / excluded, "shadow-skill")
    # A support-named dir with no SKILL.md beside it stays a normal category.
    _skill(skills, "scripts/script-category-skill")
    # A symlinked directory is listed, never entered (as rglob does).
    outside = _skill(tmp_path / "elsewhere", "linked-skill")
    os.symlink(outside.parent, skills / "linked-category")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return skills


def _rglob_reference(names):
    """The guard's previous result: rglob + the shared exclusion filter, first match per name."""
    from agent.skill_utils import get_all_skills_dirs, is_excluded_skill_path
    found = {}
    for base in get_all_skills_dirs():
        if not base.is_dir():
            continue
        for skill_md in base.rglob("SKILL.md"):
            name = skill_md.parent.name
            if names is not None and name not in names:
                continue
            if name in found or is_excluded_skill_path(skill_md, root=base):
                continue
            found[name] = os.path.realpath(skill_md.parent)
    return sorted(found.items())


@pytest.mark.parametrize("names", [None, {"locked-skill"}, {"nested-skill", "decoy-skill", "shadow-skill"},
                                   {"script-category-skill", "linked-skill", "missing"}])
def test_same_skills_as_the_full_rglob(tree, names):
    from tools.skill_lock import _skill_dirs
    assert _skill_dirs(names) == _rglob_reference(names)
    if names is None:
        assert {name for name, _ in _skill_dirs(None)} == {
            "locked-skill", "free-skill", "solo", "nested-skill", "script-category-skill"}


def test_the_walk_never_enters_a_support_or_excluded_tree(tree, monkeypatch):
    from tools.skill_lock import _skill_dirs
    listed = []
    real = os.scandir

    def scandir(path="."):
        listed.append(os.fspath(path))
        return real(path)
    monkeypatch.setattr(os, "scandir", scandir)
    assert [name for name, _ in _skill_dirs({"locked-skill"})] == ["locked-skill"]
    entered = [p for p in listed if any(part in {"references", "scripts", "assets", "templates", ".git",
                                                  "node_modules", ".archive", "__pycache__", "linked-category"}
                                        for part in os.path.relpath(p, tree).split(os.sep))]
    # Only the top-level ``scripts`` category (no SKILL.md beside it) is walked.
    assert entered and all(os.path.relpath(p, tree).split(os.sep)[0] == "scripts" for p in entered), entered
    assert len(listed) <= 12, listed  # the skill dirs and categories, not the ~250 support dirs


def test_a_locked_skill_inside_the_tree_is_still_guarded(tree, monkeypatch):
    from tools import skill_usage
    from tools.skill_lock import command_refusal, path_refusal
    assert skill_usage.set_locked("locked-skill", True)
    locked = tree / "ops/locked-skill"
    assert "locked" in path_refusal([str(locked / "references/d3/deep/new.md")])
    assert "locked" in command_refusal(f"echo x > {locked}/SKILL.md", str(tree))
    assert path_refusal([str(tree / "ops/free-skill/SKILL.md")]) is None
