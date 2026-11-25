"""Linux mount-namespace verification for read-only projected Kanban worker assets."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")
_ALLOWED_PROJECTED_NAMES = frozenset({"skills", "plugins"})


def _unescape_mountinfo_field(raw: str) -> str:
    return _OCTAL_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), raw)


def parse_mountinfo_entries(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``[(mountpoint, vfs_options), ...]`` parsed from ``/proc/self/mountinfo``."""
    entries: list[tuple[str, tuple[str, ...]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        left, sep, _right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split(" ")
        if len(fields) < 6:
            continue
        mountpoint = _unescape_mountinfo_field(fields[4])
        vfs_options = tuple(opt for opt in fields[5].split(",") if opt)
        entries.append((mountpoint, vfs_options))
    return entries


def mountpoint_is_readonly_in_mountinfo(
    target: str | Path,
    mountinfo_text: Optional[str] = None,
) -> bool:
    """True only when *target* is an exact active mountpoint with ``ro`` VFS mount options."""
    expected = str(target)
    if mountinfo_text is None:
        try:
            mountinfo_text = _MOUNTINFO_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    matched_options: Optional[tuple[str, ...]] = None
    for mountpoint, options in parse_mountinfo_entries(mountinfo_text):
        if mountpoint == expected:
            matched_options = options
    return matched_options is not None and "ro" in matched_options


def _verify_single_readonly_mount(
    attempt_home: Path,
    entry: Mapping[str, Any],
    *,
    mountinfo_text: Optional[str],
    statvfs_fn,
    access_fn,
) -> str:
    if not isinstance(entry, Mapping):
        raise RuntimeError("malformed required read-only mount entry")
    name = entry.get("name")
    target = entry.get("target")
    source_dev = entry.get("source_dev")
    source_ino = entry.get("source_ino")
    if (
        not isinstance(name, str)
        or name not in _ALLOWED_PROJECTED_NAMES
        or not isinstance(target, str)
        or not target
        or type(source_dev) is not int
        or type(source_ino) is not int
    ):
        raise RuntimeError(f"invalid required read-only mount manifest entry: {entry!r}")

    expected_target = attempt_home / name
    if target != str(expected_target):
        raise RuntimeError(
            f"mismatched read-only mount target for {name}: expected {expected_target}, got {target}"
        )
    try:
        info = expected_target.lstat()
    except OSError as exc:
        raise RuntimeError(f"missing required read-only mountpoint {expected_target}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"required read-only mountpoint is not a directory: {expected_target}")
    if info.st_dev != source_dev or info.st_ino != source_ino:
        raise RuntimeError(
            f"required read-only mountpoint {expected_target} does not match planned source inode"
        )
    if not mountpoint_is_readonly_in_mountinfo(str(expected_target), mountinfo_text=mountinfo_text):
        raise RuntimeError(
            f"required read-only view {expected_target} is not an active ro mount in /proc/self/mountinfo"
        )
    vfs_flags = statvfs_fn(str(expected_target)).f_flag
    if not (vfs_flags & getattr(os, "ST_RDONLY", 1)):
        raise RuntimeError(f"required read-only view {expected_target} does not report ST_RDONLY")
    if access_fn(str(expected_target), os.W_OK):
        raise RuntimeError(f"required read-only view {expected_target} reports writable access")
    return name


def verify_required_readonly_mounts(
    attempt_home: Path,
    required_mounts: Sequence[Mapping[str, Any]],
    *,
    mountinfo_text: Optional[str] = None,
    statvfs_fn=None,
    access_fn=None,
) -> list[str]:
    """Fail closed unless every required projected asset view is mounted read-only inside this unit."""
    if not isinstance(required_mounts, (list, tuple)):
        raise RuntimeError("required read-only mount manifest must be a list")
    statvfs = statvfs_fn or os.statvfs
    access = access_fn or os.access
    if mountinfo_text is None:
        try:
            mountinfo_text = _MOUNTINFO_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"cannot read /proc/self/mountinfo to verify read-only views: {exc}") from exc

    verified: list[str] = []
    seen: set[str] = set()
    for entry in required_mounts:
        name = _verify_single_readonly_mount(
            attempt_home,
            entry,
            mountinfo_text=mountinfo_text,
            statvfs_fn=statvfs,
            access_fn=access,
        )
        if name in seen:
            raise RuntimeError(f"duplicate required read-only mount entry: {name}")
        seen.add(name)
        verified.append(name)

    for asset_name in sorted(_ALLOWED_PROJECTED_NAMES):
        if os.path.lexists(attempt_home / asset_name) and asset_name not in seen:
            raise RuntimeError(
                f"unverified projected asset directory present in attempt home: {asset_name}"
            )
    return verified


def readonly_bind_probe_script() -> str:
    """Stdlib-only probe run inside ``systemd-run --user -p BindReadOnlyPaths=...``."""
    return (
        "import errno, os, stat, sys\n"
        "dst, exp_dev, exp_ino, canary = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]\n"
        "st = os.lstat(dst)\n"
        "if not stat.S_ISDIR(st.st_mode) or st.st_dev != exp_dev or st.st_ino != exp_ino:\n"
        "    raise SystemExit('bind source mismatch')\n"
        "if not (os.statvfs(dst).f_flag & getattr(os, 'ST_RDONLY', 1)):\n"
        "    raise SystemExit('missing ST_RDONLY')\n"
        "if os.access(dst, os.W_OK):\n"
        "    raise SystemExit('mount reports writable')\n"
        "if not os.path.isfile(os.path.join(dst, canary)):\n"
        "    raise SystemExit('missing canary file')\n"
        "matched = None\n"
        "for line in open('/proc/self/mountinfo', encoding='utf-8'):\n"
        "    left, sep, _ = line.partition(' - ')\n"
        "    if not sep:\n"
        "        continue\n"
        "    parts = left.split(' ')\n"
        "    if len(parts) >= 6 and parts[4] == dst:\n"
        "        matched = parts[5].split(',')\n"
        "if not matched or 'ro' not in matched:\n"
        "    raise SystemExit('missing ro mountinfo entry')\n"
        "try:\n"
        "    fd = os.open(os.path.join(dst, '.ro-canary-write'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n"
        "except OSError as exc:\n"
        "    if exc.errno != errno.EROFS:\n"
        "        raise SystemExit(f'expected EROFS, got errno={exc.errno}')\n"
        "else:\n"
        "    os.close(fd)\n"
        "    raise SystemExit('write unexpectedly succeeded on read-only bind mount')\n"
    )
