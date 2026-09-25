"""The generated pages a person reads for each project, and the General page.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002 and ADR 0003.
`knowledge/projects/<project>/index.md` lists one registered project's live notes
grouped by type, each as a bare `[[slug]]` link and its one-sentence summary; the
module tags those notes use; and where agents left off in each of its
repositories. `knowledge/projects/general/index.md` lists every live note whose
`project:` is absent or names no registered project (a project removed from the
map leaves its notes there). Superseded and archived notes appear nowhere.

The pages are derived, never edited: each writer that changes what they show
regenerates them inside its own transaction -- the index rebuild after a compile
and at night (`rebuild_memory_index.py`), and every project-map edit
(`project_map.manage_project`). Rendering is deterministic and a page whose bytes
would not change is not written. A stale page is deleted only while it still
carries the `generated: true` marker, so nothing hand-written is removed.

They stay out of every other reader: `index.md` is an editorial name
(`vault_editorial.EDITORIAL_NAMES`), so lint, the retrieval corpus and the claim
tree skip them, and the tracked `knowledge/index.md` lists notes only.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes
from corpus_snapshot import read_frontmatter
from page_status import is_retired
from project_map import (
    RESERVED_PROJECT_NAMES,
    ProjectMap,
    ProjectMapError,
    project_name,
    read_project_map,
)
from reliable_memory import sha256_bytes

PROJECTS_RELATIVE = "knowledge/projects"
NOTES_RELATIVE = "knowledge/notes"
PAGE_NAME = "index.md"
GENERAL = "general"
ABSENT = "absent"
MAX_PAGE_BYTES = 4 * 1024 * 1024
MAX_STATE_VALUE_CHARS = 200

TYPE_HEADINGS = {
    "decision": "Decisions",
    "pattern": "Patterns",
    "debugging": "Debugging",
    "concept": "Concepts",
    "qa": "Q&A",
    "entity": "Entities",
    "synthesis": "Syntheses",
    "comparison": "Comparisons",
    "connection": "Connections",
    "workflow": "Workflows",
    "raw-source": "Raw sources",
    "gap": "Gaps",
}
UNTYPED = "Other"
_SKIP_NAMES = frozenset({"README.md", "index.md", "log.md"})
_SUMMARY_RE = re.compile(r"^One-sentence summary:\s*(.+?)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_STATE_SECTION_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)
_STATE_ITEM_RE = re.compile(r"^- (?:`[^`]*`: )?(.+?)\s*$")
_GENERATED_RE = re.compile(r"^generated:\s*true\s*$", re.MULTILINE)
# Obsidian makes `#tag` a link when the tag has these characters and one non-digit.
_OBSIDIAN_TAG_RE = re.compile(r"^(?=.*[^\d/])[\w/-]+$")
_UNNAMED_BRANCHES = frozenset({"unknown", "journal-rotation"})

Reader = Callable[[str], "bytes | None"]


@dataclass(frozen=True)
class PageWrite:
    """One page to write (or, with no content, delete) and what it replaces."""

    path: str
    content: bytes | None
    before: str

    def change(self):
        from markdown_transaction import MarkdownChange

        if self.content is None:
            return MarkdownChange.delete(self.path)
        if self.before == ABSENT:
            return MarkdownChange.create(self.path, self.content, max_before_bytes=MAX_PAGE_BYTES)
        return MarkdownChange.replace(self.path, self.content, max_before_bytes=MAX_PAGE_BYTES)


@dataclass(frozen=True)
class Repository:
    """One registered repository as its project page shows it."""

    project: str
    folder: str
    checkout: Path

    @property
    def relative(self) -> str:
        return f"{PROJECTS_RELATIVE}/{self.project}/{self.folder}"


@dataclass(frozen=True)
class _Note:
    slug: str
    title: str
    summary: str
    kind: str
    project: str
    tags: tuple[str, ...]


def page_path(project: str) -> str:
    return f"{PROJECTS_RELATIVE}/{project}/{PAGE_NAME}"


def is_project_page(relative: str) -> bool:
    """`knowledge/projects/<project>/index.md`, the one generated page of a project folder."""
    parts = PurePosixPath(relative).parts
    return len(parts) == 4 and parts[:2] == ("knowledge", "projects") and parts[3] == PAGE_NAME


# --- reading ---------------------------------------------------------------------


def _disk_reader(vault: Path) -> Reader:
    def read(relative: str) -> bytes | None:
        try:
            return read_stable_bytes(
                Path(vault) / relative, MAX_KNOWLEDGE_PAGE_BYTES, label="project page source"
            )
        except FileNotFoundError:
            return None

    return read


def _overlaid(read: Reader, overlay: Mapping[str, bytes | None]) -> Reader:
    def overlaid(relative: str) -> bytes | None:
        if relative in overlay:
            return overlay[relative]
        return read(relative)

    return overlaid


def _disk_notes(vault: Path) -> dict[str, bytes]:
    notes = Path(vault) / NOTES_RELATIVE
    if not notes.is_dir():
        return {}
    read = _disk_reader(vault)
    found = {}
    for path in sorted(notes.rglob("*.md")):
        relative = path.relative_to(vault).as_posix()
        content = read(relative)
        if content is not None:
            found[relative] = content
    return found


def _repositories(vault: Path, project_map: ProjectMap) -> list[Repository]:
    from work_state import placements

    return [
        Repository(placement.project, placement.folder, placement.repository)
        for placement in placements(vault, project_map=project_map)
    ]


def _note(relative: str, content: bytes) -> _Note | None:
    """What a page lists for one note, or None when the note is not live."""
    path = PurePosixPath(relative)
    if path.name in _SKIP_NAMES or "archive" in path.parts[2:-1]:
        return None
    frontmatter = read_frontmatter(content)
    body = content[frontmatter.body_start :].decode("utf-8", errors="replace")
    fields = frontmatter.mapping or _fallback_fields(content[: frontmatter.body_start])
    if is_retired(fields.get("status")):
        return None
    return _Note(
        slug=path.stem,
        title=_line(_text(fields.get("title")) or _first(_H1_RE, body) or path.stem),
        summary=_line(_first(_SUMMARY_RE, body)),
        kind=_text(fields.get("type")).strip().casefold(),
        project=project_name(_text(fields.get("project"))) if fields.get("project") else "",
        tags=_tags(fields.get("tags")),
    )


def _fallback_fields(head: bytes) -> dict[str, str]:
    """`type:` and `status:` read line by line from metadata YAML could not parse."""
    text = head.decode("utf-8", errors="replace")
    return {
        name: match.group(1).strip().strip("\"'")
        for name in ("type", "status")
        if (match := re.search(rf"^{name}:\s*(.+?)\s*$", text, re.MULTILINE))
    }


def _text(value: object) -> str:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _line(text: str, limit: int | None = None) -> str:
    joined = " ".join(text.split())
    if limit is None or len(joined) <= limit:
        return joined
    return joined[: limit - 1].rstrip() + "…"


def _tags(value: object) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list):
        return ()
    tags = (_line(_text(item)).lstrip("#") for item in items)
    return tuple(dict.fromkeys(tag for tag in tags if tag))


# --- rendering -------------------------------------------------------------------


def _frontmatter_lines(title: str, project: str | None) -> list[str]:
    lines = ["---", "type: project-context", f"title: {json.dumps(title, ensure_ascii=False)}"]
    if project is not None:
        lines.append(f"project: {json.dumps(project, ensure_ascii=False)}")
    return [*lines, "generated: true", "---"]


def _section_order(kinds: set[str]) -> list[str]:
    fixed = [kind for kind in TYPE_HEADINGS if kind in kinds]
    unknown = sorted(kind for kind in kinds if kind and kind not in TYPE_HEADINGS)
    return [*fixed, *unknown, *([""] if "" in kinds else [])]


def _heading(kind: str) -> str:
    if not kind:
        return UNTYPED
    return TYPE_HEADINGS.get(kind, kind.replace("-", " ").capitalize())


def _note_lines(notes: Sequence[_Note]) -> list[str]:
    if not notes:
        return ["No live notes yet.", ""]
    lines: list[str] = []
    for kind in _section_order({note.kind for note in notes}):
        listed = sorted(
            (note for note in notes if note.kind == kind),
            key=lambda note: (note.title.casefold(), note.slug),
        )
        lines.append(f"## {_heading(kind)}")
        lines.extend(_note_line(note) for note in listed)
        lines.append("")
    return lines


def _note_line(note: _Note) -> str:
    if not note.summary:
        return f"- [[{note.slug}]]"
    return f"- [[{note.slug}]] — {note.summary}"


def _module_lines(notes: Sequence[_Note]) -> list[str]:
    counts = Counter(tag for note in notes for tag in note.tags)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))
    lines = ["## Modules"]
    if not ranked:
        return [*lines, "- None yet", ""]
    for tag, count in ranked:
        label = f"#{tag}" if _OBSIDIAN_TAG_RE.fullmatch(tag) else tag
        lines.append(f"- {label} ({count} {'note' if count == 1 else 'notes'})")
    return [*lines, ""]


def _state_sections(content: bytes) -> dict[str, list[str]]:
    text = content.decode("utf-8", errors="replace")
    matches = list(_STATE_SECTION_RE.finditer(text))
    sections: dict[str, list[str]] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        items = []
        for line in text[match.end() : end].splitlines():
            item = _STATE_ITEM_RE.match(line.strip())
            if item is not None and item.group(1) != "None":
                items.append(_line(item.group(1), MAX_STATE_VALUE_CHARS))
        sections[match.group(1).strip().casefold()] = items
    return sections


def _last_branch(journal: bytes | None) -> str:
    """The branch the newest checkpoint that knew one was recorded on."""
    if not journal:
        return ""
    for line in reversed(journal.decode("utf-8", errors="replace").split("\n")):
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line.removesuffix("\r"))
        except ValueError:
            continue
        provenance = event.get("provenance") if isinstance(event, dict) else None
        branch = provenance.get("branch") if isinstance(provenance, dict) else None
        if isinstance(branch, str) and branch.strip() and branch not in _UNNAMED_BRANCHES:
            return _line(branch, MAX_STATE_VALUE_CHARS).replace("`", "'")
    return ""


def _repository_lines(repository: Repository, read: Reader, *, detailed: bool) -> list[str]:
    state_link = f"{repository.relative.removeprefix('knowledge/')}/state"
    lines = [
        f"### {repository.folder}",
        f"- Main checkout: `{repository.checkout.as_posix()}`",
    ]
    state = read(f"{repository.relative}/state.md")
    if state is None:
        return [*lines, "- No work recorded yet.", ""]
    lines.append(f"- Work state: [[{state_link}]]")
    if not detailed:
        return [*lines, ""]
    branch = _last_branch(read(f"{repository.relative}/journal.md"))
    if branch:
        lines.append(f"- Branch: `{branch}`")
    sections = _state_sections(state)
    task = sections.get("current task", [])
    lines.append(f"- Current task: {task[-1] if task else 'none recorded'}")
    blockers = sections.get("open blockers", [])
    if blockers:
        lines.append(f"- Open blockers: {len(blockers)}, newest: {blockers[-1]}")
    else:
        lines.append("- Open blockers: none")
    return [*lines, ""]


def _work_state_lines(
    repositories: Sequence[Repository], read: Reader, *, detailed: bool
) -> list[str]:
    lines = ["## Work state"]
    if not repositories:
        return [*lines, "No repository is attached yet.", ""]
    for repository in repositories:
        lines.extend(_repository_lines(repository, read, detailed=detailed))
    return lines


def _encoded(lines: list[str]) -> bytes:
    output = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
    if len(output) > MAX_PAGE_BYTES:
        raise ValueError("project page output exceeds limit")
    return output


def _project_page(
    project: str,
    notes: Sequence[_Note],
    repositories: Sequence[Repository],
    read: Reader,
    *,
    detailed: bool = True,
) -> bytes:
    lines = [
        *_frontmatter_lines(project, project),
        f"# {project}",
        "",
        f"One-sentence summary: What the memory knows about {project}: its live notes "
        "by type, its modules, and where agents left off in each of its repositories.",
        "",
        "> Generated from the notes, the project map and each repository's work state. "
        "Do not edit this file directly.",
        "",
        *_note_lines(notes),
        *_module_lines(notes),
        *_work_state_lines(repositories, read, detailed=detailed),
    ]
    return _encoded(lines)


def _general_page(notes: Sequence[_Note]) -> bytes:
    lines = [
        *_frontmatter_lines("General", None),
        "# General",
        "",
        "One-sentence summary: The live notes that belong to no registered project, by type.",
        "",
        "> Generated from the notes and the project map. Do not edit this file directly.",
        "",
        *_note_lines(notes),
        *_module_lines(notes),
    ]
    return _encoded(lines)


def _publishable(render: Callable[[bool], bytes]) -> bytes:
    """The page with its work-state lines, or without them when those would not publish.

    A compile publishes under the model-output guard, and work state is event text
    (a task, a failed command). One flagged line there must not stop a compile, so
    the page falls back to links.
    """
    from model_dlp import DLPContentBlocked, require_safe_publication

    page = render(True)
    try:
        require_safe_publication(page)
    except DLPContentBlocked:
        return render(False)
    return page


def render_pages(
    notes: Mapping[str, bytes],
    project_map: ProjectMap,
    repositories: Sequence[Repository],
    read: Reader,
    *,
    guarded: bool = False,
) -> dict[str, bytes]:
    """Every project page and the General page, by vault-relative path."""
    registered = [name for name in project_map.names() if name not in RESERVED_PROJECT_NAMES]
    listed = [note for note in map(_note_from_item, sorted(notes.items())) if note is not None]
    by_project: dict[str, list[_Note]] = {name: [] for name in registered}
    general: list[_Note] = []
    for note in listed:
        by_project.get(note.project, general).append(note)
    pages = {page_path(GENERAL): _general_page(general)}
    for name in registered:
        own = [item for item in repositories if item.project == name]

        def render(detailed: bool, name: str = name, own: list[Repository] = own) -> bytes:
            return _project_page(name, by_project[name], own, read, detailed=detailed)

        pages[page_path(name)] = _publishable(render) if guarded else render(True)
    return pages


def _note_from_item(item: tuple[str, bytes]) -> _Note | None:
    relative, content = item
    if not relative.startswith(f"{NOTES_RELATIVE}/") or not relative.endswith(".md"):
        return None
    return _note(relative, content)


# --- what to write ---------------------------------------------------------------


def _existing_pages(vault: Path) -> list[str]:
    projects = Path(vault) / PROJECTS_RELATIVE
    if not projects.is_dir():
        return []
    return sorted(
        page_path(entry.name)
        for entry in projects.iterdir()
        if entry.is_dir() and not entry.is_symlink() and (entry / PAGE_NAME).is_file()
    )


def _is_generated(content: bytes) -> bool:
    frontmatter = read_frontmatter(content)
    head = content[: frontmatter.body_start].decode("utf-8", errors="replace")
    return _GENERATED_RE.search(head) is not None


def page_writes(
    vault: Path,
    *,
    notes: Mapping[str, bytes] | None = None,
    project_map: ProjectMap | None = None,
    repositories: Sequence[Repository] | None = None,
    overlay: Mapping[str, bytes | None] | None = None,
    guarded: bool = False,
) -> list[PageWrite]:
    """The project pages that differ from what is on disk, and the stale ones to delete.

    `notes` is the note tree the pages describe (vault-relative path to bytes), read
    from disk when absent. `overlay` is what an enclosing transaction writes under
    `knowledge/projects/` (None for a path it deletes), so a page lists the work state
    as it will be once that transaction commits. An unreadable map writes nothing:
    it would otherwise empty every project page into General. `guarded` is for a
    writer publishing under the model-output guard (the compile).
    """
    vault = Path(vault)
    if not (vault / PROJECTS_RELATIVE).is_dir():
        return []
    if project_map is None:
        try:
            project_map = read_project_map(vault)
        except (OSError, ProjectMapError):
            return []
    read = _overlaid(_disk_reader(vault), overlay or {})
    if repositories is None:
        repositories = _repositories(vault, project_map)
    desired = render_pages(
        _disk_notes(vault) if notes is None else notes,
        project_map,
        repositories,
        read,
        guarded=guarded,
    )
    writes = []
    for path in sorted({*desired, *_existing_pages(vault)}):
        before = read(path)
        content = desired.get(path)
        if content == before or (content is None and not _is_generated(before or b"")):
            continue
        writes.append(PageWrite(path, content, ABSENT if before is None else sha256_bytes(before)))
    return writes
