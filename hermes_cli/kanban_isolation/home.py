"""Plan and materialize an isolated attempt home for a card-local Kanban worker.

Reusable large asset trees (``skills``, ``plugins``) are NEVER copied per attempt;
they are validated via metadata-only traversal and projected read-only into the
attempt's mount namespace via systemd ``BindReadOnlyPaths=``.

Tiny mutable inputs (``config.yaml``, ``.env``, ``auth.json``, ``models_dev_cache.json``,
``hooks``, ``locales``, ``skins``, ``memories``, ``SOUL.md``) are privately copied so
attempt writes never mutate the owning profile or shared fleet root.

Explicit top-level relative aliases to ``<fleet-root>/<same-public-asset>`` are
accepted ONLY for standard named profiles (``<fleet-root>/profiles/<name>``) and
ONLY for the declared public assets (``skills``, ``plugins``, ``memories``, ``SOUL.md``).
"""

from __future__ import annotations

import os
import posixpath
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_dir_acl import make_user_dir

READONLY_PROJECTED_ASSETS: tuple[str, ...] = ("skills", "plugins")
COPY_ALIAS_ALLOWED_ASSETS: tuple[str, ...] = ("memories", "SOUL.md")
PRIVATE_ONLY_INPUTS: tuple[str, ...] = (
    "config.yaml",
    ".env",
    "hooks",
    "locales",
    "skins",
    "auth.json",
    "models_dev_cache.json",
)
_DIR_INPUTS = frozenset({"skills", "plugins", "hooks", "locales", "skins", "memories"})
_FILE_INPUTS = frozenset({"config.yaml", ".env", "SOUL.md", "auth.json", "models_dev_cache.json"})
_PRIVATE_COPY_INPUTS: tuple[str, ...] = (*PRIVATE_ONLY_INPUTS, *COPY_ALIAS_ALLOWED_ASSETS)
_INPUTS: tuple[str, ...] = (*_PRIVATE_COPY_INPUTS, *READONLY_PROJECTED_ASSETS)

_FORBIDDEN_BIND_CHARS = frozenset("%:\"'\\")
_MAX_INTERNAL_SYMLINK_HOPS = 40


@dataclass(frozen=True)
class AssetTreeStats:
    """Metadata-only counts produced while validating a projected read-only asset tree."""

    files: int
    dirs: int
    links: int


@dataclass(frozen=True)
class ReadOnlyProjection:
    """Validated read-only bind projection for one reusable asset directory."""

    name: str
    source: Path
    target: Path
    source_dev: int
    source_ino: int
    bind_spec: str
    aliased: bool
    stats: AssetTreeStats

    def to_manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": str(self.source),
            "target": str(self.target),
            "source_dev": self.source_dev,
            "source_ino": self.source_ino,
        }


@dataclass(frozen=True)
class PrivateCopyInput:
    """Validated private copy input for one attempt-local file or directory."""

    name: str
    source: Path
    target: Path
    is_dir: bool
    source_dev: int
    source_ino: int
    aliased: bool


@dataclass(frozen=True)
class PublicAssetValidation:
    """Pure metadata-only validation summary for declared public profile assets."""

    profile_home: Path
    fleet_root: Optional[Path]
    readonly_projections: tuple[ReadOnlyProjection, ...]
    copy_assets: tuple[PrivateCopyInput, ...]


@dataclass(frozen=True)
class AttemptHomePlan:
    """Complete immutable plan for materializing and mounting one attempt home."""

    attempt_home: Path
    profile_home: Path
    fleet_root: Optional[Path]
    readonly_projections: tuple[ReadOnlyProjection, ...]
    private_copies: tuple[PrivateCopyInput, ...]


def validate_systemd_bind_path(path: Path) -> str:
    """Reject paths containing characters that are ambiguous in systemd ``BindReadOnlyPaths=``."""
    raw = str(path)
    if not raw or not raw.startswith("/"):
        raise ValueError(f"unsafe systemd BindReadOnlyPaths path (must be absolute): {raw!r}")
    if posixpath.normpath(raw) != raw:
        raise ValueError(f"unsafe systemd BindReadOnlyPaths path (must be canonical): {raw!r}")
    for ch in raw:
        if ch in _FORBIDDEN_BIND_CHARS or ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"unsafe systemd BindReadOnlyPaths path contains unsupported character {ch!r}: {raw!r}"
            )
    return raw


def format_bind_readonly_path(source: Path, target: Path) -> str:
    """Build an unambiguous ``<source>:<target>`` property value for ``BindReadOnlyPaths=``."""
    return f"{validate_systemd_bind_path(source)}:{validate_systemd_bind_path(target)}"


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("unsafe isolated attempt directory")


def _standard_named_profile_fleet_root(profile_home: Path) -> Optional[Path]:
    """Return ``<fleet-root>`` iff *profile_home* is a canonical ``<fleet-root>/profiles/<name>``."""
    from hermes_cli.profiles import validate_profile_name

    try:
        info = profile_home.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        return None
    profiles_dir = profile_home.parent
    if profiles_dir.name != "profiles":
        return None
    try:
        profiles_info = profiles_dir.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(profiles_info.st_mode):
        return None
    fleet_root = profiles_dir.parent
    if fleet_root == profiles_dir:
        return None
    try:
        fleet_info = fleet_root.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(fleet_info.st_mode):
        return None
    profile_name = profile_home.name
    if profile_name == "default":
        return None
    try:
        validate_profile_name(profile_name)
    except ValueError:
        return None
    resolved_fleet = fleet_root.resolve(strict=True)
    if resolved_fleet / "profiles" / profile_name != profile_home.resolve(strict=True):
        return None
    return resolved_fleet


def _is_canonical_fleet_relative_alias(raw_target: str, asset_name: str) -> bool:
    """True iff *raw_target* is an explicit relative path stepping directly to ``../../<asset_name>``."""
    if not raw_target or "\x00" in raw_target or os.path.isabs(raw_target):
        return False
    parts = [segment for segment in raw_target.split("/") if segment not in ("", ".")]
    return parts == ["..", "..", asset_name]


def _resolve_internal_relative_link(
    asset_root: Path,
    asset_name: str,
    rel_parent: tuple[str, ...],
    link_name: str,
    raw_target: str,
) -> None:
    """Verify a symlink inside *asset_root* is relative and resolves strictly within *asset_root*."""
    rel_label = "/".join((*rel_parent, link_name))
    if not raw_target or "\x00" in raw_target or os.path.isabs(raw_target):
        raise ValueError(
            f"unsupported linked asset inside {asset_name}: absolute or empty symlink {rel_label!r} -> {raw_target!r}"
        )
    pending = raw_target.split("/")
    cur_parts = list(rel_parent)
    hops = 0

    while pending:
        seg = pending.pop(0)
        if seg in ("", "."):
            continue
        if seg == "..":
            if not cur_parts:
                raise ValueError(
                    f"unsupported linked asset inside {asset_name}: escaping symlink {rel_label!r} -> {raw_target!r}"
                )
            cur_parts.pop()
            continue
        next_parts = [*cur_parts, seg]
        candidate = asset_root.joinpath(*next_parts)
        try:
            cst = candidate.lstat()
        except OSError as exc:
            raise ValueError(
                f"unsupported linked asset inside {asset_name}: unresolved symlink {rel_label!r} -> {raw_target!r}"
            ) from exc
        if stat.S_ISLNK(cst.st_mode):
            hops += 1
            if hops > _MAX_INTERNAL_SYMLINK_HOPS:
                raise ValueError(
                    f"unsupported linked asset inside {asset_name}: symlink loop at {rel_label!r}"
                )
            hop_target = os.readlink(candidate)
            if not hop_target or "\x00" in hop_target or os.path.isabs(hop_target):
                raise ValueError(
                    f"unsupported linked asset inside {asset_name}: chained absolute symlink at {rel_label!r}"
                )
            pending = hop_target.split("/") + pending
            continue
        if pending and not stat.S_ISDIR(cst.st_mode):
            raise ValueError(
                f"unsupported linked asset inside {asset_name}: symlink {rel_label!r} traverses non-directory"
            )
        cur_parts = next_parts

    final_path = asset_root.joinpath(*cur_parts) if cur_parts else asset_root
    try:
        final_st = final_path.lstat()
    except OSError as exc:
        raise ValueError(
            f"unsupported linked asset inside {asset_name}: unresolved target for {rel_label!r}"
        ) from exc
    if not (stat.S_ISREG(final_st.st_mode) or stat.S_ISDIR(final_st.st_mode)):
        raise ValueError(
            f"unsupported linked or special asset inside {asset_name}: {rel_label!r} targets special file"
        )


def _validate_readonly_asset_tree(asset_root: Path, asset_name: str) -> AssetTreeStats:
    """Metadata-only validation of a shared read-only asset tree (zero file reads/hashes)."""
    files = 0
    dirs = 0
    links = 0
    visited: set[tuple[int, int]] = set()

    def _walk_dir(dir_fd: int, rel_parent: tuple[str, ...]) -> None:
        nonlocal files, dirs, links
        dir_st = os.fstat(dir_fd)
        key = (dir_st.st_dev, dir_st.st_ino)
        if key in visited:
            raise ValueError(f"unsupported cyclic directory inside {asset_name}")
        visited.add(key)

        for entry_name in sorted(os.listdir(dir_fd)):
            st = os.stat(entry_name, dir_fd=dir_fd, follow_symlinks=False)
            mode = st.st_mode
            if stat.S_ISREG(mode):
                files += 1
            elif stat.S_ISDIR(mode):
                dirs += 1
                child_fd = os.open(entry_name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    _walk_dir(child_fd, (*rel_parent, entry_name))
                finally:
                    os.close(child_fd)
            elif stat.S_ISLNK(mode):
                links += 1
                raw_target = os.readlink(entry_name, dir_fd=dir_fd)
                _resolve_internal_relative_link(asset_root, asset_name, rel_parent, entry_name, raw_target)
            else:
                rel_label = "/".join((*rel_parent, entry_name))
                raise ValueError(
                    f"unsupported special file inside {asset_name}: {rel_label!r}"
                )

    root_fd = os.open(asset_root, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _walk_dir(root_fd, ())
    finally:
        os.close(root_fd)
    return AssetTreeStats(files=files, dirs=dirs, links=links)


def _validate_copy_dir_tree(dir_path: Path, input_name: str) -> None:
    """Metadata-only check that a copied directory tree contains only regular files and directories."""
    def _walk(dir_fd: int, rel_parent: tuple[str, ...]) -> None:
        for entry_name in sorted(os.listdir(dir_fd)):
            st = os.stat(entry_name, dir_fd=dir_fd, follow_symlinks=False)
            mode = st.st_mode
            if stat.S_ISREG(mode):
                continue
            if stat.S_ISDIR(mode):
                child_fd = os.open(entry_name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    _walk(child_fd, (*rel_parent, entry_name))
                finally:
                    os.close(child_fd)
            else:
                rel_label = "/".join((*rel_parent, entry_name))
                raise ValueError(f"unsupported linked or special profile input: {input_name}/{rel_label}")

    root_fd = os.open(dir_path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _walk(root_fd, ())
    finally:
        os.close(root_fd)


def _resolve_profile_input_source(
    source_fd: int,
    profile_home: Path,
    fleet_root: Optional[Path],
    name: str,
) -> tuple[Path, os.stat_result, bool]:
    """Resolve one top-level profile input using strict ownership/alias rules (metadata-only)."""
    st = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
    expect_dir = name in _DIR_INPUTS
    allow_fleet_alias = name in READONLY_PROJECTED_ASSETS or name in COPY_ALIAS_ALLOWED_ASSETS

    if stat.S_ISLNK(st.st_mode):
        if not allow_fleet_alias:
            raise ValueError(f"unsupported linked or special profile input: {name}")
        if fleet_root is None:
            raise ValueError(
                f"unsupported linked profile input {name}: top-level alias requires a standard named profile"
            )
        raw_target = os.readlink(name, dir_fd=source_fd)
        if not _is_canonical_fleet_relative_alias(raw_target, name):
            raise ValueError(
                f"unsupported linked profile input {name}: alias {raw_target!r} must target ../../{name}"
            )
        canonical_target = fleet_root / name
        try:
            target_st = canonical_target.lstat()
        except OSError as exc:
            raise ValueError(
                f"unsupported linked profile input {name}: target {canonical_target} does not exist"
            ) from exc
        if stat.S_ISLNK(target_st.st_mode):
            raise ValueError(
                f"unsupported linked profile input {name}: chained symlink at {canonical_target}"
            )
        if expect_dir and not stat.S_ISDIR(target_st.st_mode):
            raise ValueError(f"unsupported linked or special profile input: {name} must target a directory")
        if not expect_dir and not stat.S_ISREG(target_st.st_mode):
            raise ValueError(f"unsupported linked or special profile input: {name} must target a regular file")
        if canonical_target.resolve(strict=True) != canonical_target:
            raise ValueError(f"unsupported linked profile input {name}: non-canonical fleet target")
        return canonical_target, target_st, True

    if expect_dir and stat.S_ISDIR(st.st_mode):
        return profile_home / name, st, False
    if not expect_dir and stat.S_ISREG(st.st_mode):
        return profile_home / name, st, False
    raise ValueError(f"unsupported linked or special profile input: {name}")


def _plan_inputs(
    profile_home: Path,
    *,
    attempt_home: Optional[Path],
    input_names: tuple[str, ...],
) -> tuple[Path, Optional[Path], tuple[ReadOnlyProjection, ...], tuple[PrivateCopyInput, ...]]:
    resolved_profile = Path(profile_home).expanduser().resolve(strict=True)
    fleet_root = _standard_named_profile_fleet_root(resolved_profile)
    resolved_attempt = Path(attempt_home).expanduser().resolve() if attempt_home is not None else None

    projections: list[ReadOnlyProjection] = []
    copies: list[PrivateCopyInput] = []

    source_fd = os.open(resolved_profile, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        available = set(os.listdir(source_fd))
        for name in input_names:
            if name not in available:
                continue
            resolved_source, target_st, aliased = _resolve_profile_input_source(
                source_fd, resolved_profile, fleet_root, name
            )
            if name in READONLY_PROJECTED_ASSETS:
                stats = _validate_readonly_asset_tree(resolved_source, name)
                target_path = (resolved_attempt / name) if resolved_attempt is not None else Path("/") / name
                if resolved_attempt is not None:
                    bind_spec = format_bind_readonly_path(resolved_source, target_path)
                else:
                    validate_systemd_bind_path(resolved_source)
                    bind_spec = ""
                projections.append(
                    ReadOnlyProjection(
                        name=name,
                        source=resolved_source,
                        target=target_path,
                        source_dev=target_st.st_dev,
                        source_ino=target_st.st_ino,
                        bind_spec=bind_spec,
                        aliased=aliased,
                        stats=stats,
                    )
                )
            else:
                is_dir = name in _DIR_INPUTS
                if is_dir:
                    _validate_copy_dir_tree(resolved_source, name)
                target_path = (resolved_attempt / name) if resolved_attempt is not None else Path("/") / name
                copies.append(
                    PrivateCopyInput(
                        name=name,
                        source=resolved_source,
                        target=target_path,
                        is_dir=is_dir,
                        source_dev=target_st.st_dev,
                        source_ino=target_st.st_ino,
                        aliased=aliased,
                    )
                )
    finally:
        os.close(source_fd)

    return resolved_profile, fleet_root, tuple(projections), tuple(copies)


def validate_public_asset_sources(
    profile_home: Path,
    *,
    attempt_home: Optional[Path] = None,
) -> PublicAssetValidation:
    """Pure read-only metadata validator for ONLY public profile assets (skills, plugins, memories, SOUL.md).

    Safe for coordinator preflight checks on real profile metadata: never opens or reads
    any file contents, and never inspects ``.env``, ``auth.json``, or ``config.yaml``.
    """
    public_names = (*READONLY_PROJECTED_ASSETS, *COPY_ALIAS_ALLOWED_ASSETS)
    resolved_profile, fleet_root, projections, copies = _plan_inputs(
        profile_home, attempt_home=attempt_home, input_names=public_names
    )
    return PublicAssetValidation(
        profile_home=resolved_profile,
        fleet_root=fleet_root,
        readonly_projections=projections,
        copy_assets=copies,
    )


def plan_attempt_home(attempt_home: Path, profile_home: Path) -> AttemptHomePlan:
    """Pure read-only metadata planner for one isolated Kanban attempt home."""
    resolved_attempt = Path(attempt_home).expanduser().resolve()
    resolved_profile, fleet_root, projections, copies = _plan_inputs(
        profile_home, attempt_home=resolved_attempt, input_names=_INPUTS
    )
    return AttemptHomePlan(
        attempt_home=resolved_attempt,
        profile_home=resolved_profile,
        fleet_root=fleet_root,
        readonly_projections=projections,
        private_copies=copies,
    )


def _snapshot(parent_fd: int, name: str, target: Path) -> None:
    mode = os.stat(name, dir_fd=parent_fd, follow_symlinks=False).st_mode
    if stat.S_ISDIR(mode):
        source_fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            target.mkdir(mode=0o700)
            for child in os.listdir(source_fd):
                _snapshot(source_fd, child, target / child)
        finally:
            os.close(source_fd)
    elif stat.S_ISREG(mode):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        with os.fdopen(fd, "rb") as source, target.open("xb") as output:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError("profile input changed during snapshot")
            os.fchmod(output.fileno(), 0o700 if mode & 0o100 else 0o600)
            shutil.copyfileobj(source, output)
    else:
        raise ValueError(f"unsupported linked or special profile input: {name}")


def _copy_planned_input(entry: PrivateCopyInput, destination: Path) -> None:
    parent_fd = os.open(entry.source.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.stat(entry.source.name, dir_fd=parent_fd, follow_symlinks=False)
        if st.st_dev != entry.source_dev or st.st_ino != entry.source_ino:
            raise ValueError(f"profile input changed during snapshot: {entry.name}")
        _snapshot(parent_fd, entry.source.name, destination)
    finally:
        os.close(parent_fd)


def _check_private_input(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        for child in path.iterdir():
            _check_private_input(child)
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError("unsafe link in existing attempt home")


def _check_existing_mountpoint(path: Path) -> None:
    _private_directory(path)
    if os.listdir(path):
        raise PermissionError(f"unexpected content in projected asset mountpoint: {path.name}")


def _verify_existing_attempt_home(attempt_home: Path) -> None:
    _private_directory(attempt_home)
    for name in _PRIVATE_COPY_INPUTS:
        if os.path.lexists(attempt_home / name):
            _check_private_input(attempt_home / name)
    for name in READONLY_PROJECTED_ASSETS:
        if os.path.lexists(attempt_home / name):
            _check_existing_mountpoint(attempt_home / name)


def _ensure_private_ancestors(attempt_home: Path) -> Path:
    root = attempt_home.parent
    for directory in (root.parent.parent, root.parent, root):
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        _private_directory(directory)
    return root


def materialize_attempt_home(plan: AttemptHomePlan) -> Path:
    """Materialize private copies and empty RO mountpoints for a validated :class:`AttemptHomePlan`."""
    attempt_home = plan.attempt_home
    root = _ensure_private_ancestors(attempt_home)
    if os.path.lexists(attempt_home):
        _verify_existing_attempt_home(attempt_home)
        planned_ro = {proj.name for proj in plan.readonly_projections}
        for name in READONLY_PROJECTED_ASSETS:
            mountpoint = attempt_home / name
            if name in planned_ro and not os.path.lexists(mountpoint):
                mountpoint.mkdir(mode=0o700)
            elif name not in planned_ro and os.path.lexists(mountpoint):
                raise PermissionError(f"unexpected projected asset mountpoint in existing attempt home: {name}")
        return attempt_home

    with tempfile.TemporaryDirectory(dir=root) as tmp:
        staging = Path(tmp) / "snapshot"
        staging.mkdir(mode=0o700)
        for copy_entry in plan.private_copies:
            _copy_planned_input(copy_entry, staging / copy_entry.name)
        for proj in plan.readonly_projections:
            (staging / proj.name).mkdir(mode=0o700)
        staging.rename(attempt_home)
    return attempt_home


def prepare_attempt_home_plan(attempt_home: Path, profile_home: Path) -> AttemptHomePlan:
    """Validate an existing attempt home (if any), plan inputs, and materialize private copies/mountpoints."""
    resolved_attempt = Path(attempt_home).expanduser().resolve()
    _ensure_private_ancestors(resolved_attempt)
    if os.path.lexists(resolved_attempt):
        _verify_existing_attempt_home(resolved_attempt)
    plan = plan_attempt_home(resolved_attempt, profile_home)
    materialize_attempt_home(plan)
    return plan


def prepare_attempt_home(attempt_home: Path, profile_home: Path) -> Path:
    """Prepare the attempt-private home directory and return its resolved path."""
    return prepare_attempt_home_plan(attempt_home, profile_home).attempt_home
