"""The project map: which repositories each registered project is made of.

One private Markdown file, `knowledge/projects/project-map.md`, that the owner
reads and edits in Obsidian and agents edit through the `manage_project` MCP
tool (ADR 0002). Each `## <project>` heading names a project; each bullet under
it is the main checkout of one of its repositories:

    ## product-a

    - C:/work/backend
    - C:/work/frontend

The parser tolerates hand edits: prose, blank lines, other headings, bullets in
backticks, either slash, trailing slashes, and (on Windows) any case. Edits are
line edits, so whatever the owner wrote around the entries survives them.
Nothing here reads the map for journals or work state yet; that is issue #14.
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAP_RELATIVE_PATH = "knowledge/projects/project-map.md"
MAX_MAP_BYTES = 256 * 1024
MAX_NAME_CHARS = 128
RESERVED_PROJECT_NAMES = frozenset({"general"})
ACTIONS = ("create", "attach", "detach", "rename", "remove", "list")

_NAME_UNSAFE = re.compile(r"[\s_/\\:*?\"<>|]+")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*?)\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_ENTRY_WRAPPERS = "`'\"<>"

_NEW_MAP = """---
type: project-context
---
# Project map

One-sentence summary: The projects the owner has registered and the repositories each one is made of.

Each `## <project>` heading names a project. Each `- <path>` bullet under it is the
main checkout of one of its repositories. Edit it here, or ask an agent to register
a project. `doctor` reports entries it cannot use.
"""


class ProjectMapError(ValueError):
    """A request the map cannot honour, with a stable code the caller can act on."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class Project:
    name: str
    repositories: tuple[Path, ...]


@dataclass(frozen=True)
class MapProblem:
    code: str
    line: int
    message: str
    project: str | None = None
    repository: str | None = None

    def as_data(self) -> dict:
        return {
            key: value
            for key, value in (
                ("code", self.code),
                ("line", self.line),
                ("message", self.message),
                ("project", self.project),
                ("repository", self.repository),
            )
            if value is not None
        }


@dataclass
class _Section:
    name: str
    start: int
    end: int
    entries: list[tuple[int, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ProjectMap:
    projects: tuple[Project, ...]
    problems: tuple[MapProblem, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(project.name for project in self.projects)

    def project_named(self, name: str) -> Project | None:
        wanted = project_name(name)
        return next((item for item in self.projects if item.name == wanted), None)

    def project_of(self, repository: Path | str) -> str | None:
        """The project that owns a repository's main checkout, or None."""
        key = repository_key(repository)
        for project in self.projects:
            if any(repository_key(path) == key for path in project.repositories):
                return project.name
        return None

    def as_data(self) -> list[dict]:
        return [
            {
                "name": project.name,
                "repositories": [path.as_posix() for path in project.repositories],
            }
            for project in self.projects
        ]


def project_name(text: str) -> str:
    """The owner's words as a slug usable as a folder name, or empty."""
    from project_journal import portable_slug

    lowered = unicodedata.normalize("NFC", str(text)).strip().lower()
    cleaned = _NAME_UNSAFE.sub("-", lowered).strip("-.")[:MAX_NAME_CHARS].strip("-.")
    slug = portable_slug(cleaned)
    return slug if any(character.isalnum() for character in slug) else ""


def repository_key(path: Path | str) -> str:
    """One spelling per checkout: either slash, no trailing slash, Windows case folded."""
    return os.path.normcase(os.path.normpath(str(_entry_path(str(path)))))


def _entry_path(text: str) -> Path:
    path = Path(text.replace("\\", "/"))
    try:
        return path.expanduser()
    except RuntimeError:
        return path


def _entry_text(raw: str) -> str:
    return raw.strip().strip(_ENTRY_WRAPPERS).strip()


def _body_start(lines: list[str]) -> int:
    """The first line after YAML frontmatter, or 0 when there is none."""
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return index + 1
    return 0


def _sections(lines: list[str]) -> list[_Section]:
    sections: list[_Section] = []
    current: _Section | None = None
    fenced = False
    for index in range(_body_start(lines), len(lines)):
        line = lines[index]
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) <= 2:
            if current is not None:
                current.end = index
            current = None
            if len(heading.group(1)) == 2:
                current = _Section(project_name(heading.group(2)), index, len(lines))
                sections.append(current)
            continue
        bullet = _BULLET.match(line)
        if current is not None and bullet and _entry_text(bullet.group(1)):
            current.entries.append((index, _entry_text(bullet.group(1))))
    return sections


def _section_problem(section: _Section, seen: dict[str, int]) -> MapProblem | None:
    line = section.start + 1
    if not section.name:
        return MapProblem("invalid_name", line, "heading does not name a usable project")
    if section.name in RESERVED_PROJECT_NAMES:
        return MapProblem(
            "reserved_name",
            line,
            f"'{section.name}' is reserved for notes without a project",
            project=section.name,
        )
    if section.name in seen:
        return MapProblem(
            "duplicate_project",
            line,
            f"project '{section.name}' is also declared on line {seen[section.name]}",
            project=section.name,
        )
    return None


def _built_map(sections: list[_Section]) -> ProjectMap:
    """Projects in file order; a duplicate heading merges into the first one."""
    problems: list[MapProblem] = []
    seen: dict[str, int] = {}
    owners: dict[str, str] = {}
    repositories: dict[str, list[Path]] = {}
    for section in sections:
        problem = _section_problem(section, seen)
        if problem is not None:
            problems.append(problem)
            if problem.code != "duplicate_project":
                continue
        seen.setdefault(section.name, section.start + 1)
        listed = repositories.setdefault(section.name, [])
        for index, text in section.entries:
            key = repository_key(text)
            owner = owners.setdefault(key, section.name)
            if owner != section.name:
                problems.append(
                    MapProblem(
                        "repository_in_two_projects",
                        index + 1,
                        f"repository is already in project '{owner}'",
                        project=section.name,
                        repository=text,
                    )
                )
                continue
            if all(repository_key(path) != key for path in listed):
                listed.append(_entry_path(text))
    projects = tuple(Project(name, tuple(paths)) for name, paths in repositories.items())
    return ProjectMap(projects, tuple(problems))


def parse_project_map(text: str) -> ProjectMap:
    return _built_map(_sections(text.splitlines()))


def map_path(vault: Path) -> Path:
    return Path(vault) / MAP_RELATIVE_PATH


def _read_map_bytes(vault: Path) -> bytes | None:
    path = map_path(vault)
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_MAP_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > MAX_MAP_BYTES:
        raise ProjectMapError("map_too_large", "the project map is larger than 256 KiB")
    return content


def _decoded(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ProjectMapError("map_unreadable", "the project map is not UTF-8") from error


def read_project_map(vault: Path) -> ProjectMap:
    """The registered projects; an absent map registers none."""
    content = _read_map_bytes(vault)
    if content is None:
        return ProjectMap((), ())
    return parse_project_map(_decoded(content))


def _filesystem_problems(vault: Path, sections: list[_Section]) -> list[MapProblem]:
    from session_start_project_state import repository_of

    projects_dir = Path(vault) / "knowledge" / "projects"
    problems = []
    for section in sections:
        for index, text in section.entries:
            path = _entry_path(text)
            if not path.is_absolute() or not path.is_dir():
                problems.append(
                    MapProblem(
                        "missing_repository",
                        index + 1,
                        "repository path is not an existing absolute directory",
                        project=section.name or None,
                        repository=text,
                    )
                )
                continue
            checkout = repository_of(path, projects_dir)
            if repository_key(checkout) != repository_key(path) or not (path / ".git").exists():
                problems.append(
                    MapProblem(
                        "not_repository_root",
                        index + 1,
                        "path is not the main checkout of a git repository",
                        project=section.name or None,
                        repository=text,
                    )
                )
    return problems


def project_map_problems(vault: Path) -> list[MapProblem]:
    """Every entry the map cannot use, structural and on disk, in file order."""
    content = _read_map_bytes(vault)
    if content is None:
        return []
    sections = _sections(_decoded(content).splitlines())
    problems = [*_built_map(sections).problems, *_filesystem_problems(vault, sections)]
    return sorted(problems, key=lambda problem: problem.line)


# --- editing -------------------------------------------------------------------


class _Draft:
    """The map's lines under edit, re-parsed after every change."""

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()
        self.sections = _sections(self.lines)

    def _reparse(self) -> None:
        self.sections = _sections(self.lines)

    def text(self) -> str:
        while self.lines and not self.lines[-1].strip():
            self.lines.pop()
        return "\n".join(self.lines) + "\n"

    def parsed(self) -> ProjectMap:
        return _built_map(self.sections)

    def sections_named(self, name: str) -> list[_Section]:
        return [section for section in self.sections if section.name == name]

    def add_project(self, name: str) -> None:
        while self.lines and not self.lines[-1].strip():
            self.lines.pop()
        self.lines.extend(["", f"## {name}"])
        self._reparse()

    @staticmethod
    def _ends_a_list(line: str) -> bool:
        """Prose right after a bullet would render as part of it."""
        return bool(line.strip()) and not _BULLET.match(line)

    def add_repository(self, name: str, repository: Path) -> None:
        section = self.sections_named(name)[0]
        bullet = f"- {repository.as_posix()}"
        if section.entries:
            at = section.entries[-1][0] + 1
            self.lines.insert(at, bullet)
        else:
            at = section.start + 1
            if at < len(self.lines) and not self.lines[at].strip():
                at += 1
                self.lines.insert(at, bullet)
            else:
                self.lines[at:at] = ["", bullet]
                at += 1
        following = at + 1
        if following < len(self.lines) and self._ends_a_list(self.lines[following]):
            self.lines.insert(following, "")
        self._reparse()

    def _entries_of(self, repository: Path | str) -> list[tuple[int, str]]:
        key = repository_key(repository)
        return [
            (index, section.name)
            for section in self.sections
            for index, text in section.entries
            if repository_key(text) == key
        ]

    def owners(self, repository: Path | str) -> list[str]:
        return list(dict.fromkeys(name for _index, name in self._entries_of(repository)))

    def remove_repository(self, repository: Path | str) -> list[str]:
        doomed = self._entries_of(repository)
        for index, _name in sorted(doomed, reverse=True):
            del self.lines[index]
        self._reparse()
        return list(dict.fromkeys(name for _index, name in doomed))

    def rename(self, old: str, new: str) -> None:
        for section in self.sections_named(old):
            self.lines[section.start] = f"## {new}"
        self._reparse()

    def remove_project(self, name: str) -> None:
        for section in sorted(self.sections_named(name), key=lambda item: -item.start):
            del self.lines[section.start : section.end]
            before = section.start - 1
            if 0 <= before < len(self.lines) - 1 and not self.lines[before].strip():
                if not self.lines[before + 1].strip():
                    del self.lines[before]
        self._reparse()


def _required_name(name: object) -> str:
    slug = project_name(name) if isinstance(name, str) else ""
    if not slug:
        raise ProjectMapError(
            "invalid_name", "a project name needs at least one letter or digit"
        )
    if slug in RESERVED_PROJECT_NAMES:
        raise ProjectMapError(
            "reserved_name", f"'{slug}' is reserved for notes without a project"
        )
    return slug


def _known_project(parsed: ProjectMap, name: object) -> str:
    slug = project_name(name) if isinstance(name, str) else ""
    if slug and parsed.project_named(slug) is not None:
        return slug
    known = ", ".join(parsed.names()) or "none"
    raise ProjectMapError(
        "unknown_project",
        f"no project named '{slug or name}' is registered; registered: {known}",
        known=list(parsed.names()),
    )


def _absolute_directory(directory: object) -> Path:
    if not isinstance(directory, str) or not directory.strip():
        raise ProjectMapError("directory_required", "directory is required for this action")
    path = _entry_path(directory.strip())
    if not path.is_absolute():
        raise ProjectMapError("directory_not_absolute", "directory must be an absolute path")
    return path


def resolve_repository(vault: Path, directory: object) -> Path:
    """The main checkout of the repository holding `directory`.

    A subfolder or a worktree names the same repository as its main checkout; a
    directory in no git repository, the vault, or the home directory names none.
    """
    from session_start_project_state import NotAProject, project_identity

    path = _absolute_directory(directory)
    if not path.is_dir():
        raise ProjectMapError("directory_not_found", "directory does not exist")
    try:
        _slug, repository = project_identity(path.resolve(), Path(vault) / "knowledge" / "projects")
    except NotAProject as error:
        raise ProjectMapError("not_a_repository", str(error)) from error
    if not (repository / ".git").exists():
        raise ProjectMapError(
            "not_a_repository",
            "the directory is not inside a git repository, so it cannot join a project",
        )
    return repository


@dataclass
class _Outcome:
    project: str | None
    message: str
    changed: bool = True
    extra: dict = field(default_factory=dict)


def _attach(draft: _Draft, name: str, repository: Path) -> _Outcome:
    if draft.owners(repository) == [name]:
        return _Outcome(name, f"repository already belongs to '{name}'", changed=False)
    moved_from = [owner for owner in draft.remove_repository(repository) if owner != name]
    draft.add_repository(name, repository)
    extra = {"repository": repository.as_posix()}
    if moved_from:
        extra["moved_from"] = moved_from
        return _Outcome(
            name,
            f"repository moved from '{', '.join(moved_from)}' to '{name}'",
            extra=extra,
        )
    return _Outcome(name, f"repository attached to '{name}'", extra=extra)


def _create(vault: Path, draft: _Draft, request: dict) -> _Outcome:
    name = _required_name(request.get("name"))
    if draft.parsed().project_named(name) is not None or draft.sections_named(name):
        raise ProjectMapError("project_exists", f"project '{name}' is already registered")
    repository = None
    if request.get("directory") is not None:
        repository = resolve_repository(vault, request["directory"])
    draft.add_project(name)
    if repository is None:
        return _Outcome(name, f"project '{name}' registered")
    attached = _attach(draft, name, repository)
    attached.message = f"project '{name}' registered; {attached.message}"
    return attached


def _attach_action(vault: Path, draft: _Draft, request: dict) -> _Outcome:
    name = _known_project(draft.parsed(), request.get("name"))
    return _attach(draft, name, resolve_repository(vault, request.get("directory")))


def _detach(vault: Path, draft: _Draft, request: dict) -> _Outcome:
    """A path that is listed is detached as written, even when it no longer exists."""
    listed = _absolute_directory(request.get("directory"))
    repository = listed
    if not draft.owners(listed):
        if not listed.is_dir():
            raise ProjectMapError(
                "not_attached", "the path is not listed in any registered project"
            )
        repository = resolve_repository(vault, request.get("directory"))
    detached_from = draft.remove_repository(repository)
    if not detached_from:
        raise ProjectMapError(
            "not_attached", "the repository does not belong to any registered project"
        )
    return _Outcome(
        detached_from[0],
        f"repository detached from '{', '.join(detached_from)}'",
        extra={"repository": repository.as_posix(), "detached_from": detached_from},
    )


def _rename(_vault: Path, draft: _Draft, request: dict) -> _Outcome:
    old = _known_project(draft.parsed(), request.get("name"))
    new = _required_name(request.get("new_name"))
    if new == old:
        return _Outcome(old, f"project is already named '{old}'", changed=False)
    if draft.parsed().project_named(new) is not None or draft.sections_named(new):
        raise ProjectMapError("project_exists", f"project '{new}' is already registered")
    draft.rename(old, new)
    return _Outcome(new, f"project '{old}' renamed to '{new}'", extra={"renamed_from": old})


def _remove(_vault: Path, draft: _Draft, request: dict) -> _Outcome:
    name = _known_project(draft.parsed(), request.get("name"))
    project = draft.parsed().project_named(name)
    released = [path.as_posix() for path in project.repositories] if project else []
    draft.remove_project(name)
    message = f"project '{name}' removed"
    if released:
        message += f"; its {len(released)} repositories belong to no project now"
    return _Outcome(name, message, extra={"detached": released})


_EDITS = {
    "create": _create,
    "attach": _attach_action,
    "detach": _detach,
    "rename": _rename,
    "remove": _remove,
}


def _write_map(
    vault: Path,
    state_root: Path,
    before: bytes | None,
    after: bytes,
    action: str,
    *,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> None:
    from markdown_transaction import (
        ABSENT,
        MarkdownChange,
        PreconditionChangedError,
        active_or_legacy_coordinator,
        sha256_bytes,
    )

    coordinator = active_or_legacy_coordinator(Path(vault), Path(state_root))
    if before is None:
        change = MarkdownChange.create(MAP_RELATIVE_PATH, after, max_before_bytes=MAX_MAP_BYTES)
        expected = ABSENT
    else:
        change = MarkdownChange.replace(MAP_RELATIVE_PATH, after, max_before_bytes=MAX_MAP_BYTES)
        expected = sha256_bytes(before)
    try:
        record = coordinator.prepare(
            [change],
            operation_id=f"project-map:{action}:{uuid.uuid4().hex}",
            preconditions={MAP_RELATIVE_PATH: expected},
            deadline=deadline,
            cancelled=cancelled,
        )
    except PreconditionChangedError as error:
        raise ProjectMapError(
            "map_changed", "the project map changed while it was being edited; try again"
        ) from error
    coordinator.apply(record.id, deadline=deadline, cancelled=cancelled)


def manage_project(
    vault: Path,
    state_root: Path,
    request: dict,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Apply one registration action to the map and describe the result.

    `request` carries `action` plus the action's own fields: `name`, `new_name`,
    and `directory` (an absolute path inside the repository, usually the agent's
    working directory). Raises `ProjectMapError` for a request it refuses.
    """
    action = request.get("action")
    before = _read_map_bytes(vault)
    if action == "list":
        parsed = read_project_map(vault)
        return {
            "status": "ok",
            "action": "list",
            "map": MAP_RELATIVE_PATH,
            "projects": parsed.as_data(),
            "problems": [problem.as_data() for problem in project_map_problems(vault)],
        }
    edit = _EDITS.get(action)
    if edit is None:
        raise ProjectMapError("unknown_action", f"unknown action: {action}")
    draft = _Draft(_NEW_MAP if before is None else _decoded(before))
    outcome = edit(vault, draft, request)
    if outcome.changed:
        after = draft.text().encode("utf-8")
        _write_map(
            vault, state_root, before, after, action, deadline=deadline, cancelled=cancelled
        )
    return {
        "status": "ok",
        "action": action,
        "project": outcome.project,
        "changed": outcome.changed,
        "message": outcome.message,
        **outcome.extra,
        "map": MAP_RELATIVE_PATH,
        "projects": draft.parsed().as_data(),
    }
