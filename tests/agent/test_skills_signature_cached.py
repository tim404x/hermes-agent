import os
import time
from collections import Counter
from pathlib import Path

from agent import skill_commands, skill_index
from hermes_constants import get_hermes_home
from tools import skills_tool


def _signature(root):
    return skills_tool._skills_scan_signature([root], set())


def _settle(root, monkeypatch):
    old = time.time() - 10
    for path, _, _ in os.walk(root):
        os.utime(path, (old, old))
    now = time.time_ns
    monkeypatch.setattr(time, "time_ns", lambda: now() + 10_000_000_000)


def _count_scans(monkeypatch):
    scans = Counter()
    scan = os.scandir

    def counted(path):
        scans[os.path.realpath(path)] += 1
        return scan(path)

    monkeypatch.setattr(os, "scandir", counted)
    return scans


def test_quiet_tree_does_not_relist_and_nested_changes_are_seen(tmp_path, monkeypatch):
    category = tmp_path / "ml" / "training"
    category.mkdir(parents=True)
    _settle(tmp_path, monkeypatch)
    initial = _signature(tmp_path)
    scans = _count_scans(monkeypatch)
    assert initial == _signature(tmp_path) == _signature(tmp_path)
    assert not scans
    skill = category / "first"
    skill.mkdir()
    added = _signature(tmp_path)
    marker = skill / "SKILL.md"
    marker.touch()
    marked = _signature(tmp_path)
    marker.unlink()
    unmarked = _signature(tmp_path)
    renamed = skill.rename(category / "second")
    moved = _signature(tmp_path)
    renamed.rmdir()
    removed = _signature(tmp_path)
    assert all(left != right for left, right in zip(
        (initial, added, marked, unmarked, moved), (added, marked, unmarked, moved, removed)))


def test_marker_added_in_same_directory_tick_changes_signature(tmp_path, monkeypatch):
    skill = tmp_path / "category" / "skill"
    skill.mkdir(parents=True)
    key = skill_index.stat_key(skill)
    stat_key = skill_index.stat_key
    monkeypatch.setattr(skill_index, "stat_key", lambda path: key if Path(path) == skill else stat_key(path))
    before = _signature(tmp_path)
    (skill / "SKILL.md").touch()
    os.utime(skill, ns=(key[-2], key[-2]))
    assert before != _signature(tmp_path)


def test_published_scopes_share_one_walk_even_while_racy(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    (root / "category").mkdir(parents=True)
    homes = [tmp_path / name for name in ("first", "second")]
    for home in homes:
        home.mkdir()
        (home / "skills").symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(skill_commands, "_skill_commands_by_key", {("cli", str(home), None): {} for home in homes})
    monkeypatch.setattr(skills_tool, "skill_search_dirs", lambda: (
        [(0, get_hermes_home() / "skills")] if get_hermes_home() in homes else [], None))
    monkeypatch.setattr(skills_tool, "_get_disabled_skill_names", set)
    scans = _count_scans(monkeypatch)
    first = skill_commands.published_scopes_signature()
    assert scans == {str(root): 1, str(root / "category"): 1}
    (root / "category" / "new").mkdir()
    second = skill_commands.published_scopes_signature()
    assert all(first[index] != second[index] for index in (1, 2))


def test_atomic_usage_rewrite_is_not_a_catalog_change(tmp_path):
    usage = tmp_path / ".usage.json"
    usage.write_text("{}")
    before = _signature(tmp_path)
    replacement = tmp_path / ".usage_staging.tmp"
    replacement.write_text('{"count": 1}')
    (tmp_path / ".usage.json.lock").touch()
    assert _signature(tmp_path) == before
    replacement.replace(usage)
    assert _signature(tmp_path) == before


def test_pruned_symlink_kind_and_atomic_skill_replacement_are_visible(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (root / ".hub").symlink_to(target, target_is_directory=True)
    marker = root / "SKILL.md"
    marker.touch()
    _settle(root, monkeypatch)
    before = _signature(root)
    target.rmdir()
    target.touch()
    after = _signature(root)
    assert after != before
    new = tmp_path / "new.md"
    new.write_text("new command")
    new.replace(marker)
    assert _signature(root) != after
