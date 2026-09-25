"""Where a registered repository's work state lives, and the journal key it carries.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002. A repository
the project map registers keeps its append-only `journal.md` and the `state.md`
generated from it at `knowledge/projects/<project>/<repository>/`, where
`<repository>` is the main checkout's folder name with the collision rules of
`session_start_project_state.repository_folder` applied among the project's own
repositories. A directory that resolves to no registered repository has no place
here: every writer asks `placement_of` first and writes nothing when it refuses.

A journal is named by a key, the `project` field its events carry and the key of
its checkpoint rows. The key is read back from the folder's journal rather than
derived from the folder, so a folder that moves (the repository joins another
project, the project is renamed) keeps its journal and its sequence. A folder
with no journal yet gets a fresh key: the folder name plus a digest of the main
checkout, with a suffix when an earlier journal under that key was deleted and
left committed checkpoints behind (detach and attach again).
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from project_journal import JOURNAL_HEADER, ProjectStore, recorded_journal_key
from project_map import ProjectMap, ProjectMapError, read_project_map, repository_key
from session_start_project_state import (
    NotAProject,
    repository_folder,
    working_repository,
)

PROJECTS_RELATIVE = "knowledge/projects"
KEY_DIGEST_CHARS = 8
MAX_KEY_BASE_CHARS = 64
MAX_KEY_INCARNATIONS = 99
MAX_FIRST_EVENT_BYTES = 256 * 1024


class Unregistered(NotAProject):
    """The directory's repository belongs to no registered project."""


@dataclass(frozen=True)
class Placement:
    """One registered repository's place under `knowledge/projects/`."""

    project: str
    repository: Path
    folder: str

    @property
    def relative(self) -> str:
        """`<project>/<repository>`, relative to `knowledge/projects/`."""
        return f"{self.project}/{self.folder}"

    def directory(self, vault: Path) -> Path:
        return projects_dir(vault) / self.project / self.folder


def projects_dir(vault: Path) -> Path:
    return Path(vault) / PROJECTS_RELATIVE


def _registered(vault: Path, project_map: ProjectMap | None) -> ProjectMap:
    if project_map is not None:
        return project_map
    try:
        return read_project_map(vault)
    except ProjectMapError:
        return ProjectMap((), ())


def _placed(vault: Path, project: str, repository: Path) -> Placement:
    parent = projects_dir(vault) / project
    return Placement(project, repository, repository_folder(repository, parent, projects_dir(vault)))


def placement_of(
    vault: Path, directory: Path, *, project_map: ProjectMap | None = None
) -> Placement:
    """The place of the registered repository an agent works in.

    Raises `NotAProject` for the vault, temporary directories and the home
    directory, and `Unregistered` (also a `NotAProject`) for any other directory
    whose repository the project map does not list.
    """
    repository = working_repository(Path(directory), projects_dir(vault))
    project = _registered(vault, project_map).project_of(repository)
    if project is None:
        raise Unregistered("the repository belongs to no registered project")
    return _placed(vault, project, repository)


UNREGISTERED_TAG = "-"


def daily_tag(vault: Path, directory: Path | str) -> str:
    """What a daily-log breadcrumb names for the directory it came from.

    `<project>/<repository>` for a registered repository, `-` for anything else:
    a breadcrumb from unregistered work names no project (ADR 0002).
    """
    try:
        return placement_of(vault, Path(directory).resolve()).relative
    except (OSError, ValueError, RuntimeError):
        return UNREGISTERED_TAG


def placements(vault: Path, *, project_map: ProjectMap | None = None) -> list[Placement]:
    """Every registered repository's place, in map order; unusable entries are skipped."""
    found: list[Placement] = []
    for project in _registered(vault, project_map).projects:
        for repository in project.repositories:
            if not repository.is_absolute():
                continue
            try:
                found.append(_placed(vault, project.name, repository))
            except (OSError, ValueError):
                continue
    return found


def recorded_key(folder: Path) -> str | None:
    """The key the folder's journal carries, from its first event; None without one."""
    journal = Path(folder) / "journal.md"
    try:
        with journal.open("rb") as stream:
            head = stream.read(len(JOURNAL_HEADER.encode("utf-8")) + MAX_FIRST_EVENT_BYTES)
    except OSError:
        return None
    return recorded_journal_key(head)


def fresh_key(placement: Placement, has_history: Callable[[str], bool]) -> str:
    """The key a repository's first journal takes: unused by any committed checkpoint."""
    digest = hashlib.sha256(repository_key(placement.repository).encode("utf-8")).hexdigest()
    base = f"{placement.folder[:MAX_KEY_BASE_CHARS].rstrip('-.')}-{digest[:KEY_DIGEST_CHARS]}"
    candidate = base
    for incarnation in range(2, MAX_KEY_INCARNATIONS + 1):
        if not has_history(candidate):
            break
        candidate = f"{base}-{incarnation}"
    return candidate


def journal_key(vault: Path, placement: Placement, store: ProjectStore) -> str:
    """The key the repository's journal carries, or the one its first journal will."""
    recorded = recorded_key(placement.directory(vault))
    if recorded is not None:
        return recorded
    return fresh_key(placement, store.has_committed_checkpoints)


def placement_of_key(vault: Path, key: str, store: ProjectStore) -> Placement | None:
    """The registered repository whose journal carries `key`, or None."""
    for placement in placements(vault):
        if journal_key(vault, placement, store) == key:
            return placement
    return None


def _locate(vault: Path) -> Callable[[ProjectStore, str], str]:
    def locate(store: ProjectStore, key: str) -> str:
        placement = placement_of_key(vault, key, store)
        if placement is None:
            raise Unregistered(f"work state {key!r} belongs to no registered repository")
        return placement.relative

    return locate


def operator_store(vault: Path, state_root: Path) -> ProjectStore:
    """The store an operator's repair runs against, named by journal key.

    A key of a registered repository is found where it lives; any other key is
    read as a journal from before the project layout, in its flat `<key>/` folder.
    """
    root = Path(vault)

    def locate(store: ProjectStore, key: str) -> str:
        placement = placement_of_key(root, key, store)
        return placement.relative if placement is not None else key

    return ProjectStore(vault, state_root, locate=locate)


def work_state_store(
    vault: Path, state_root: Path, placement: Placement | None = None
) -> tuple[ProjectStore, str | None]:
    """A journal store that writes only where a registered repository lives.

    With a placement, also its journal key, already located in the store.
    """
    store = ProjectStore(vault, state_root, locate=_locate(Path(vault)))
    if placement is None:
        return store, None
    key = journal_key(vault, placement, store)
    store.place(key, placement.relative)
    return store, key
