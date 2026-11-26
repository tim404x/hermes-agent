"""The per-tool-call skill guards reuse ONE cached walk of each skills root (agent.skill_index).

Gateway loop stall, 24 Nov 2026: the skill-lock guard (every terminal / write_file / patch call) and
skill_view's candidate lookup each walked the whole skills tree per call (~16k files, 70-330 ms of
GIL-held Python). With ~30 tool threads busy they starved the gateway's asyncio loop into >10 s
stalls (``ws write slow (loop stalled >10.0s)``) until the liveness watchdog killed it (exit 75).
The cache must answer without walking, and still see every change on the very next call.
"""
import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest


def _skill(root: Path, rel: str, name: str | None = None) -> Path:
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name or path.name}\ndescription: x\n---\nbody\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "hh"
    skills = home / "skills"
    _skill(skills, "ops/locked-skill")
    _skill(skills, "ops/free-skill")
    _skill(skills, "training/accelerate", name="huggingface-accelerate")
    for index in range(20):
        (skills / "ops/locked-skill/references" / f"r{index}").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from tools import skill_usage
    _clear_index()
    assert skill_usage.set_locked("locked-skill", True)
    return home


def _clear_index():
    try:
        from agent import skill_index
    except ImportError:  # the pre-index code (these tests reproduce its per-call walks)
        return
    skill_index.clear()


class _WalkCounter:
    """Counts every directory listing/walk primitive the lookups could use."""

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        real_scandir, real_walk, real_rglob = os.scandir, os.walk, Path.rglob

        def scandir(path="."):
            self.calls.append(f"scandir {os.fspath(path)}")
            return real_scandir(path)

        def walk(top, *a, **k):
            self.calls.append(f"walk {os.fspath(top)}")
            return real_walk(top, *a, **k)

        def rglob(self_path, pattern, *a, **k):
            self.calls.append(f"rglob {self_path} {pattern}")
            return real_rglob(self_path, pattern, *a, **k)

        monkeypatch.setattr(os, "scandir", scandir)
        monkeypatch.setattr(os, "walk", walk)
        monkeypatch.setattr(Path, "rglob", rglob)


def _settled(monkeypatch):
    """Pretend every stat is old enough to trust (no racy re-listing), as on a quiet live tree."""
    try:
        from agent import skill_index
    except ImportError:
        return
    monkeypatch.setattr(skill_index, "RACY_NS", 0)


def test_lock_guards_do_not_walk_on_the_second_call(home, monkeypatch):
    from tools.skill_lock import command_refusal, path_refusal
    _settled(monkeypatch)
    locked = home / "skills/ops/locked-skill"
    assert "locked" in command_refusal(f"echo x > {locked}/SKILL.md", str(home))
    walks = _WalkCounter(monkeypatch)
    assert "locked" in command_refusal(f"echo x > {locked}/SKILL.md", str(home))
    assert command_refusal("ls -la /tmp", "/tmp") is None
    assert "locked" in path_refusal([str(locked / "SKILL.md")])
    assert path_refusal([str(home / "skills/ops/free-skill/SKILL.md")]) is None
    assert walks.calls == []


def test_skill_view_lookup_does_not_walk_or_reparse_on_the_second_call(home, monkeypatch):
    from tools import skills_tool
    _settled(monkeypatch)
    roots, _ = skills_tool.skill_search_dirs()
    error, skill_dir, _md = skills_tool.locate_skill("huggingface-accelerate", None, roots)
    assert error is None and skill_dir.name == "accelerate"
    parsed = []
    real = skills_tool._safe_frontmatter
    monkeypatch.setattr(skills_tool, "_safe_frontmatter", lambda *a, **k: parsed.append(a) or real(*a, **k))
    walks = _WalkCounter(monkeypatch)
    error, skill_dir, _md = skills_tool.locate_skill("huggingface-accelerate", None, roots)
    assert error is None and skill_dir.name == "accelerate"
    assert walks.calls == [] and parsed == []
    # The whole tool: only the viewed skill's own support dirs are listed (linked_files), never the tree.
    assert json.loads(skills_tool.skill_view("huggingface-accelerate"))["success"]
    assert not [c for c in walks.calls if str(home) in c and "accelerate" not in c], walks.calls


def test_a_new_locked_skill_dir_is_seen_on_the_very_next_call(home):
    """A just-written directory is younger than the coarse filesystem clock can prove: it is re-listed
    (the racy rule), so even a change inside the same timestamp tick shows."""
    from tools import skill_usage
    from tools.skill_lock import command_refusal, path_refusal
    assert skill_usage.set_locked("fresh-skill", True)
    target = home / "skills/ops/fresh-skill/SKILL.md"
    assert path_refusal([str(target)]) is None  # not a skill yet
    _skill(home / "skills", "ops/fresh-skill")
    assert "fresh-skill" in path_refusal([str(target)])
    assert "fresh-skill" in command_refusal(f"rm -rf {target.parent}", str(home))


def test_a_settled_tree_still_sees_changes_through_the_stat_check(home, monkeypatch):
    """With nothing racy, the per-dir (inode, mtime, ctime) check alone catches a new skill, a rename
    and a removal."""
    from tools import skill_usage
    from tools.skill_lock import path_refusal
    _settled(monkeypatch)
    assert skill_usage.set_locked("alpha", True)
    skills = home / "skills"
    _skill(skills, "cat/beta")
    assert path_refusal([str(skills / "cat/beta/SKILL.md")]) is None
    time.sleep(0.05)  # past the filesystem timestamp tick, so the parent dir's stat moves
    (skills / "cat/beta").rename(skills / "cat/alpha")
    assert "alpha" in path_refusal([str(skills / "cat/alpha/SKILL.md")])
    time.sleep(0.05)
    nested = _skill(skills, "deep/er/still/alpha")  # a brand-new category tree
    assert "alpha" in path_refusal([str(nested / "x.md")])
    time.sleep(0.05)
    os.unlink(skills / "cat/alpha/SKILL.md")
    assert path_refusal([str(skills / "cat/alpha/SKILL.md")]) is None


def test_a_root_sidecar_rewrite_keeps_the_index(home, monkeypatch):
    """``.usage.json`` is rewritten in the skills root on every skill view: re-listing the root finds no
    relevant change, so the root is not re-walked."""
    from agent import skill_index
    from tools.skill_lock import path_refusal
    _settled(monkeypatch)
    path_refusal([str(home / "skills/ops/free-skill/SKILL.md")])
    builds = []
    real = skill_index._RootIndex.__init__
    monkeypatch.setattr(skill_index._RootIndex, "__init__", lambda s, *a: builds.append(a) or real(s, *a))
    time.sleep(0.05)
    (home / "skills/.curator_state").write_text("{}")
    assert "locked" in path_refusal([str(home / "skills/ops/locked-skill/SKILL.md")])
    assert builds == []


def test_skill_manage_write_bumps_the_generation_and_is_seen(home, monkeypatch):
    from agent import skill_index
    from tools import skill_usage
    from tools.skill_lock import _skill_dirs, path_refusal
    from tools.skill_manager_tool import skill_manage
    _settled(monkeypatch)
    assert "made-by-manage" not in dict(_skill_dirs(None))  # the index is built and cached
    before = skill_index._generation
    content = "---\nname: made-by-manage\ndescription: A skill made by skill_manage.\n---\n\n# Body\n\nSteps.\n"
    result = json.loads(skill_manage(action="create", name="made-by-manage", content=content))
    assert result.get("success"), result
    assert skill_index._generation > before  # rebuilt on next use, even inside one timestamp tick
    created = Path(result["skill_md"]).parent
    assert dict(_skill_dirs(None))["made-by-manage"] == os.path.realpath(created)
    assert skill_usage.set_locked("made-by-manage", True)  # (create resets the usage record)
    assert "made-by-manage" in path_refusal([str(created / "SKILL.md")])


def test_a_frontmatter_rename_is_seen_by_skill_view(home, monkeypatch):
    from tools import skills_tool
    roots, _ = skills_tool.skill_search_dirs()
    md = home / "skills/training/accelerate/SKILL.md"
    assert skills_tool.locate_skill("huggingface-accelerate", None, roots)[0] is None
    md.write_text(md.read_text().replace("huggingface-accelerate", "hf-accelerate-renamed"))  # in place
    assert skills_tool.locate_skill("hf-accelerate-renamed", None, roots)[0] is None
    assert skills_tool.locate_skill("huggingface-accelerate", None, roots)[0] is not None
    # Settled clock, same-size rewrite: the file's mtime/ctime still move it.
    _settled(monkeypatch)
    assert skills_tool.locate_skill("hf-accelerate-renamed", None, roots)[0] is None
    time.sleep(0.05)
    md.write_text(md.read_text().replace("hf-accelerate-renamed", "hf-accelerate-RENAMED"))
    assert skills_tool.locate_skill("hf-accelerate-RENAMED", None, roots)[0] is None


def test_flat_markdown_lookup_skips_archives_and_is_literal(home):
    from tools import skills_tool
    skills = home / "skills"
    (skills / "notes").mkdir()
    (skills / "notes/flat-skill.md").write_text("---\nname: flat-skill\ndescription: x\n---\n")
    (skills / ".archive").mkdir()
    (skills / ".archive/archived-flat.md").write_text("---\nname: archived-flat\ndescription: x\n---\n")
    roots, _ = skills_tool.skill_search_dirs()
    assert skills_tool.locate_skill("flat-skill", None, roots)[0] is None
    assert skills_tool.locate_skill("archived-flat", None, roots)[0] is not None
    assert skills_tool.locate_skill("flat-*", None, roots)[0] is not None  # no glob injection
    assert skills_tool.locate_skill("notes/flat-skill", None, roots)[0] is None


def test_profiles_and_roots_are_indexed_separately(tmp_path, monkeypatch):
    from agent import skill_index
    from tools.skill_lock import _skill_dirs
    skill_index.clear()
    for profile in ("a", "b"):
        _skill(tmp_path / profile / "skills", f"cat/only-in-{profile}")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    assert [n for n, _ in _skill_dirs(None)] == ["only-in-a"]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
    assert [n for n, _ in _skill_dirs(None)] == ["only-in-b"]


def test_a_damaged_lock_record_keeps_failing_closed(home):
    from tools.skill_lock import path_refusal
    free = home / "skills/ops/free-skill/SKILL.md"
    assert path_refusal([str(free)]) is None
    (home / "skills/.usage.json").write_text("{not json")
    for _ in range(3):
        assert "cannot be read" in path_refusal([str(free)])


def test_gateway_identity_parse_is_memoized():
    from gateway import status
    line = "/usr/bin/python3 -m hermes_cli.main gateway run --replace"
    status._gateway_command_subcommand(line)
    hits = status._gateway_command_subcommand.cache_info().hits
    assert status.looks_like_gateway_command_line(line)
    assert status._gateway_command_subcommand.cache_info().hits == hits + 1


def test_concurrent_guards_share_one_walk_and_keep_the_loop_responsive(home, monkeypatch):
    """24 threads hammering the terminal guard: one build, coalesced checks, and an asyncio loop on
    the main thread keeps waking on time (the gateway's owner loop shares this GIL)."""
    from agent import skill_index
    from tools.skill_lock import command_refusal
    skills = home / "skills"
    for index in range(100):  # a tree big enough that a walk per call would show
        _skill(skills, f"cat{index % 10}/skill-{index}")
        (skills / f"cat{index % 10}/skill-{index}/references/a/b").mkdir(parents=True)
    skill_index.clear()
    _settled(monkeypatch)
    builds, checks = [], []
    real_init, real_valid = skill_index._RootIndex.__init__, skill_index._RootIndex.still_valid
    monkeypatch.setattr(skill_index._RootIndex, "__init__", lambda s, *a: builds.append(1) or real_init(s, *a))
    monkeypatch.setattr(skill_index._RootIndex, "still_valid", lambda s: checks.append(1) or real_valid(s))
    locked = skills / "ops/locked-skill/SKILL.md"
    stop = threading.Event()
    refused = []

    def hammer():
        while not stop.is_set():
            refused.append("locked" in (command_refusal(f"echo x > {locked}", str(home)) or ""))

    async def lag_probe(seconds: float) -> list[float]:
        lags, end = [], time.monotonic() + seconds
        while time.monotonic() < end:
            t0 = time.monotonic()
            await asyncio.sleep(0.01)
            lags.append(time.monotonic() - t0 - 0.01)
        return lags

    threads = [threading.Thread(target=hammer, daemon=True) for _ in range(24)]
    for thread in threads:
        thread.start()
    try:
        lags = asyncio.run(lag_probe(1.0))
    finally:
        stop.set()
        for thread in threads:
            thread.join(10)
    assert refused and all(refused)
    assert len(builds) == 1, len(builds)
    assert len(checks) < len(refused), (len(checks), len(refused))  # coalesced, not one per call
    lags.sort()
    assert lags[-1] < 0.5, f"loop lag max {lags[-1]:.3f}s p99 {lags[int(len(lags) * 0.99)]:.3f}s"


def test_unreliable_ctime_relists_index_when_parent_mtime_is_restored(home, monkeypatch):
    from agent import skill_index

    _settled(monkeypatch)
    skills = home / "skills"
    ops = skills / "ops"
    old_stat = os.stat(ops)
    real_stat_key = skill_index.stat_key

    def windows_like_stat_key(path):
        key = real_stat_key(path)
        if key is not None and Path(path) == ops:
            return (*key[:-1], old_stat.st_ctime_ns)
        return key

    monkeypatch.setattr(skill_index, "stat_key", windows_like_stat_key)
    monkeypatch.setattr(skill_index, "stat_ctime_is_change_time", lambda: False)
    before_key = skill_index.stat_key(ops)
    assert (skills / "ops/sneaky-skill/SKILL.md") not in skill_index.root_index(skills).skill_index_files()

    _skill(skills, "ops/sneaky-skill")
    os.utime(ops, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert skill_index.stat_key(ops) == before_key
    assert (skills / "ops/sneaky-skill/SKILL.md") in skill_index.root_index(skills).skill_index_files()


@pytest.mark.platforms("windows")
def test_windows_restored_mtime_after_child_add_rebuilds_skill_index(home, monkeypatch):
    from agent import skill_index

    _settled(monkeypatch)
    skills = home / "skills"
    ops = skills / "ops"
    old_stat = os.stat(ops)
    assert (skills / "ops/sneaky-skill/SKILL.md") not in skill_index.root_index(skills).skill_index_files()

    _skill(skills, "ops/sneaky-skill")
    os.utime(ops, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert (skills / "ops/sneaky-skill/SKILL.md") in skill_index.root_index(skills).skill_index_files()
