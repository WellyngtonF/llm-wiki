"""Which repository a directory belongs to, read from what git leaves on disk.

A repository is identified by its main checkout (ADR 0002): a subfolder resolves
upward to its git root, and a linked worktree follows its `.git` pointer file to the
checkout that owns it. Lifecycle hooks run on every event, so this reads files and
never spawns `git`.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

MAX_POINTER_BYTES = 4096
GITDIR_PREFIX = "gitdir:"


def repository_root(directory: Path, *, ceilings: Iterable[Path]) -> Path:
    """The main checkout of the repository holding `directory`, else `directory`.

    The walk stops before any ceiling or any ancestor of one: a dotfiles repository
    at home, or a repository that happens to contain the vault, must not swallow
    every directory beneath it.
    """
    stops = [_resolved(ceiling) for ceiling in ceilings]
    for level in (directory, *directory.parents):
        if any(level == stop or level in stop.parents for stop in stops):
            break
        marker = level / ".git"
        if marker.is_dir():
            return level
        if marker.is_file():
            return _main_checkout(level, marker)
    return directory


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _main_checkout(level: Path, pointer: Path) -> Path:
    """The checkout a `.git` pointer file belongs to.

    A linked worktree's git dir sits at `<main>/.git/worktrees/<name>` and names
    the shared `<main>/.git` in its `commondir`. A submodule's git dir has no
    `commondir`, and a bare repository has no main checkout; both stay the
    repository at `level`.
    """
    gitdir = _pointer_target(level, pointer)
    if gitdir is None:
        return level
    common = _common_dir(gitdir)
    if common is None or common.name.lower() != ".git":
        return level
    return common.parent


def _pointer_target(level: Path, pointer: Path) -> Path | None:
    lines = _read_small(pointer).splitlines()
    first = lines[0].strip() if lines else ""
    if not first.lower().startswith(GITDIR_PREFIX):
        return None
    target = Path(first[len(GITDIR_PREFIX) :].strip())
    if not target.is_absolute():
        target = level / target
    return _resolved(target)


def _common_dir(gitdir: Path) -> Path | None:
    """The shared git dir of a linked worktree, from `commondir` or the path shape."""
    recorded = _read_small(gitdir / "commondir")
    if recorded:
        common = Path(recorded)
        return _resolved(common if common.is_absolute() else gitdir / common)
    if gitdir.parent.name == "worktrees":
        return gitdir.parent.parent
    return None


def _read_small(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            return stream.read(MAX_POINTER_BYTES).decode("utf-8", errors="ignore").strip()
    except OSError:
        return ""
