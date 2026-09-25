"""Move the vault from one folder per working directory to the projects the owner registers.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002. Before the project
map, every directory an agent worked in got a flat `knowledge/projects/<slug>/`
holding a `journal.md` and a `state.md`: subfolders of a repository, its worktrees,
scratch directories, the home directory. Nothing reads those folders any more. One
operator command, in three steps, turns them into registered projects:

1. `--propose` resolves the roots each old folder recorded (its `state.md` Project
   root and its journal events' worktrees) through repository identity: a subfolder
   or a worktree names its main checkout, and a root that no longer exists or is in
   no git repository names none. It writes two private proposals the owner edits in
   Obsidian, and writes nothing else:
   - `knowledge/projects/project-map.proposed.md`, in the project map's format: one
     project per main checkout the map does not register yet, named after its folder;
   - `knowledge/projects/note-projects.proposed.md`: one `- <note>: <project>` line
     per live note, `-` for none, with the reason. The daily entries a note's
     evidence cites vote, the most named project wins, and a tie names none, as the
     compile decides; an old entry names its work by `Project root:`, `Project slug:`
     or its breadcrumb's old slug. Without a winner, a project whose name prefixes the
     note's slug is proposed.
2. Without a flag, a dry run shows the map the proposals produce, KEEP or DELETE for
   every old folder, the notes that gain `project:`, and the checkpoint queue keys
   that will be cleared. It writes nothing.
3. `--apply` requires both proposals and does all of it in one recoverable
   transaction: registers the proposed repositories (adding to an existing map,
   never moving what it registers), moves each kept journal to
   `<project>/<repository>/` with its `state.md` generated again there, deletes every
   other old folder, writes `project:` onto notes that have none (an existing value is
   never overwritten), and removes the proposals. `markdown_transaction.py undo <id>`
   reverts it inside the 2-day undo window; after that the deletion is permanent. A
   second apply finds nothing to migrate.

A journal is named by the key its events carry, and its sequence continues from the
checkpoints committed under that key (`work_state`), so two journals cannot become
one without rewriting committed events. When several old folders resolve to one
repository, the one with most events moves and the others are deleted; when the
repository already keeps a journal in the new layout, all of them are deleted.

The pending checkpoint events `run/state.json` holds under a key that no registered
repository's journal carries can never drain; apply clears them, with their reducer
and in-flight entries, after the transaction commits. An undo does not bring them
back.

Usage:
    uv run python scripts/migrate_projects.py --propose   # write the two proposals
    uv run python scripts/migrate_projects.py             # dry run
    uv run python scripts/migrate_projects.py --apply
    uv run python scripts/migrate_projects.py --json      # machine-readable report
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from itertools import dropwhile, takewhile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes  # noqa: E402
from corpus_snapshot import read_frontmatter  # noqa: E402
from evidence_resolver import _REF_RE, EvidenceRef, daily_entries  # noqa: E402
from markdown_transaction import ABSENT, mutate_knowledge  # noqa: E402
from memory_state import ROOT, STATE_ROOT, update_state  # noqa: E402
from note_project import _BREADCRUMB, _METADATA, _TAG_FIELD  # noqa: E402
from page_status import is_retired  # noqa: E402
from project_journal import (  # noqa: E402
    legacy_state_project_root,
    parse_journal_events,
    recorded_journal_key,
    rendered_state,
)
from project_map import (  # noqa: E402
    MAP_RELATIVE_PATH,
    RESERVED_PROJECT_NAMES,
    ProjectMap,
    ProjectMapError,
    _prune_empty,
    _prune_expired_empty,
    map_text_problems,
    parse_project_map,
    project_name,
    repository_key,
    resolve_repository,
    with_repositories,
)
from rebuild_memory_index import SKIP_NAMES  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402
from work_state import Placement, fresh_key, placements, recorded_key  # noqa: E402

PROJECTS = "knowledge/projects"
MAP_PROPOSAL = f"{PROJECTS}/project-map.proposed.md"
NOTES_PROPOSAL = f"{PROJECTS}/note-projects.proposed.md"
NO_PROJECT = "-"
# What the flat layout wrote: the journal, its projection and its sealed segments.
_OLD_LAYOUT_FILE = re.compile(r"^(?:journal|state|journal\.\d{6}-\d{6})\.md$")
# An atomic write that never got renamed: `.state.md.<32 hex>.tmp`.
_WRITE_LEFTOVER = re.compile(r"^\..+\.[0-9a-f]{32}\.tmp$")
_NOTE_LINE = re.compile(r"^\s*[-*+]\s+`?([^\s:`]+)`?\s*:\s*`?([^\s`]+)`?")
_STATE_QUEUES = ("project_checkpoint_pending", "project_checkpoint_inflight")
_STATE_REDUCERS = "project_checkpoint_reducers"

MAP_PROPOSAL_HEADER = """---
type: project-context
---
# Proposed project map

One-sentence summary: The projects `scripts/migrate_projects.py --propose` proposes to register, for the owner's approval.

The same format as `project-map.md`: each `## <project>` heading names a project and
each `- <path>` bullet is the main checkout of one of its repositories. Rename a
project, move a repository under another heading to group it, or delete a bullet to
leave that repository unregistered. Then run the dry run, and `--apply`. Apply adds
these to the map; a repository the map already registers stays where it is.
"""

NOTES_PROPOSAL_HEADER = """---
type: project-context
---
# Proposed note projects

One-sentence summary: The project each existing note will carry, proposed by `scripts/migrate_projects.py --propose` for the owner's approval.

Each `- <note>: <project>` line gives that note `project:` in its frontmatter; `-`
gives it none. Change the project after the colon; the text after the dash is the
reason and is ignored. A note that already carries a project is listed for reference
and is never changed.
"""


class MigrationRefused(RuntimeError):
    """A migration step the vault's current state does not allow."""


# --- old-layout folders ----------------------------------------------------------


@dataclass
class OldFolder:
    name: str
    files: list[str]
    leftovers: list[str]
    key: str | None
    events: int
    repository: Path | None = None
    reason: str = ""

    @property
    def relative(self) -> str:
        return f"{PROJECTS}/{self.name}"


def _read(vault: Path, relative: str) -> bytes:
    return read_stable_bytes(vault / relative, MAX_KNOWLEDGE_PAGE_BYTES, label=relative)


def _journal_roots(content: bytes) -> tuple[str | None, int, list[str]]:
    """(key, number of events, worktree of each event in order) of a journal."""
    key = recorded_journal_key(content)
    if key is None:
        return None, 0, []
    try:
        events = parse_journal_events(key, content)
    except (ValueError, RuntimeError):
        events = []
        for line in content.decode("utf-8", errors="replace").split("\n"):
            try:
                event = json.loads(line) if line.startswith("{") else None
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    worktrees = []
    for event in events:
        provenance = event.get("provenance")
        worktree = provenance.get("worktree") if isinstance(provenance, dict) else None
        if isinstance(worktree, str) and worktree:
            worktrees.append(worktree)
    return key, len(events), worktrees


_REFUSALS = {
    "directory_not_found": "its recorded root no longer exists",
    "directory_not_absolute": "its recorded root is not an absolute path",
    "directory_required": "it records no root",
}


class _Resolver:
    """Repository identity, asked once per distinct recorded root."""

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self._seen: dict[str, Path | str] = {}

    def __call__(self, root: str) -> Path | str:
        """The main checkout, or the reason there is none."""
        if root not in self._seen:
            try:
                self._seen[root] = resolve_repository(self.vault, root)
            except ProjectMapError as error:
                self._seen[root] = _REFUSALS.get(error.code, str(error))
            except (OSError, ValueError, RuntimeError) as error:
                self._seen[root] = f"its recorded root cannot be read ({type(error).__name__})"
        return self._seen[root]


def _folder_repository(roots: list[str], resolve: _Resolver) -> tuple[Path | None, str]:
    """The repository most recorded roots resolve to; a tie goes to the latest root."""
    votes: Counter[str] = Counter()
    latest: dict[str, int] = {}
    checkouts: dict[str, Path] = {}
    reason = "it records no root"
    for index, root in enumerate(roots):
        resolved = resolve(root)
        if isinstance(resolved, str):
            reason = resolved
            continue
        key = repository_key(resolved)
        votes[key] += 1
        latest[key] = index
        checkouts[key] = resolved
    if not votes:
        return None, reason
    best = max(votes, key=lambda key: (votes[key], latest[key]))
    return checkouts[best], ""


def old_folders(vault: Path, project_names: set[str], resolve: _Resolver) -> list[OldFolder]:
    """Every folder of the flat layout, with the repository its roots name.

    A folder is of the flat layout when it holds a `journal.md` or a `state.md`
    itself. In a folder that is also a registered project's, only the journal files
    are the flat layout's; its other pages belong to the project.
    """
    root = vault / PROJECTS
    if not root.is_dir():
        return []
    found = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name):
        if directory.name == "_template" or directory.is_symlink() or not directory.is_dir():
            continue
        entries = [
            path
            for path in sorted(directory.iterdir(), key=lambda path: path.name)
            if path.is_file() and not path.is_symlink()
        ]
        names = {path.name for path in entries}
        if not names & {"journal.md", "state.md"}:
            continue
        owned = directory.name not in project_names
        files = [
            f"{PROJECTS}/{directory.name}/{path.name}"
            for path in entries
            if _OLD_LAYOUT_FILE.match(path.name) or (owned and path.suffix == ".md")
        ]
        leftovers = [
            f"{PROJECTS}/{directory.name}/{path.name}"
            for path in entries
            if _WRITE_LEFTOVER.match(path.name)
        ]
        found.append(_described(vault, directory.name, files, leftovers, resolve))
    return found


def _described(
    vault: Path, name: str, files: list[str], leftovers: list[str], resolve: _Resolver
) -> OldFolder:
    roots: list[str] = []
    key, events = None, 0
    if "state.md" in {Path(file).name for file in files}:
        state = _read(vault, f"{PROJECTS}/{name}/state.md").decode("utf-8", errors="replace")
        recorded = legacy_state_project_root(state)
        if recorded:
            roots.append(recorded)
    if "journal.md" in {Path(file).name for file in files}:
        key, events, worktrees = _journal_roots(_read(vault, f"{PROJECTS}/{name}/journal.md"))
        roots.extend(worktrees)
    repository, reason = _folder_repository(roots, resolve)
    return OldFolder(name, files, leftovers, key, events, repository, reason)


# --- the proposed map ------------------------------------------------------------


def _read_optional(vault: Path, relative: str) -> bytes | None:
    try:
        return _read(vault, relative)
    except FileNotFoundError:
        return None


def _decoded(content: bytes | None) -> str | None:
    return None if content is None else content.decode("utf-8-sig")


def _proposed_name(repository: Path, taken: set[str]) -> str:
    base = project_name(repository.name) or "project"
    candidates = [base, project_name(f"{repository.name}-{repository.parent.name}")]
    candidates += [f"{base}-{number}" for number in range(2, 1000)]
    return next(name for name in candidates if name and name not in taken)


def _proposed_entries(current: ProjectMap, folders: list[OldFolder]) -> list[tuple[str, Path]]:
    repositories: dict[str, Path] = {}
    for folder in folders:
        if folder.repository is not None:
            repositories.setdefault(repository_key(folder.repository), folder.repository)
    taken = set(current.names()) | RESERVED_PROJECT_NAMES
    entries = []
    for key in sorted(repositories, key=lambda key: (repositories[key].name.lower(), key)):
        repository = repositories[key]
        if current.project_of(repository) is not None:
            continue
        name = _proposed_name(repository, taken)
        taken.add(name)
        entries.append((name, repository))
    return entries


# --- note projects ---------------------------------------------------------------


@dataclass
class Note:
    slug: str
    relative: str
    content: bytes
    existing: str | None
    has_frontmatter: bool


def live_notes(vault: Path) -> list[Note]:
    """Flat notes that are knowledge rather than history."""
    notes_dir = vault / "knowledge" / "notes"
    if not notes_dir.is_dir():
        return []
    notes = []
    for path in sorted(notes_dir.glob("*.md")):
        if path.name in SKIP_NAMES or path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(vault).as_posix()
        content = _read(vault, relative)
        frontmatter = read_frontmatter(content)
        if is_retired(frontmatter.mapping.get("status")):
            continue
        existing = frontmatter.mapping.get("project")
        existing = str(existing).strip() if existing not in (None, "") else None
        notes.append(Note(path.stem, relative, content, existing, frontmatter.body_start > 0))
    return notes


class _EntryProjects:
    """The project a daily entry's work belongs to under a given map."""

    def __init__(self, project_map: ProjectMap, folders: list[OldFolder], resolve: _Resolver):
        self.map = project_map
        self.slugs = {folder.name: folder.repository for folder in folders if folder.repository}
        self.resolve = resolve

    def of(self, entry: bytes) -> str | None:
        lines = [line.strip() for line in entry.decode("utf-8", "replace").splitlines()[1:]]
        body = list(dropwhile(lambda line: not line, lines))
        crumb = _BREADCRUMB.match(body[0]) if body else None
        if crumb is not None:
            return self._tag(crumb[1], crumb[2])
        fields = {match[1]: match[2] for match in takewhile(bool, map(_METADATA.match, body))}
        if "Repository" in fields:
            return self.map.project_of(fields["Repository"])
        if "Project root" in fields:
            return self._root(fields["Project root"])
        if "Project slug" in fields:
            return self._slug(fields["Project slug"])
        return None

    def _tag(self, kind: str, text: str) -> str | None:
        parts = text.split(" | ")
        count, index = _TAG_FIELD[kind]
        if len(parts) != count:
            return None
        tag = parts[index].strip()
        if "/" in tag:
            project = tag.split("/", 1)[0]
            return project if self.map.project_named(project) is not None else None
        return self._slug(tag)

    def _root(self, root: str) -> str | None:
        resolved = self.resolve(root)
        return None if isinstance(resolved, str) else self.map.project_of(resolved)

    def _slug(self, slug: str) -> str | None:
        repository = self.slugs.get(slug)
        return None if repository is None else self.map.project_of(repository)


class _DailyLogs:
    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self._logs: dict[str, tuple[bytes, list] | None] = {}

    def entry(self, reference: EvidenceRef) -> tuple[str, int, bytes] | None:
        """The cited entry; a daily log only grows, so a later digest keeps its offsets."""
        if reference.daily_id not in self._logs:
            try:
                content = _read(self.vault, f"knowledge/daily/{reference.daily_id}.md")
                self._logs[reference.daily_id] = (content, daily_entries(content))
            except (OSError, ValueError):
                self._logs[reference.daily_id] = None
        log = self._logs[reference.daily_id]
        if log is None:
            return None
        content, entries = log
        for _block, start, end in entries:
            if start <= reference.byte_start < end:
                return reference.daily_id, start, content[start:end]
        return None


def _cited_entries(note: Note, logs: _DailyLogs) -> dict[tuple[str, int], bytes]:
    entries = {}
    for match in _REF_RE.finditer(note.content.decode("utf-8", errors="replace")):
        try:
            found = logs.entry(EvidenceRef.parse(match.group(0)))
        except ValueError:
            continue
        if found is not None:
            entries[found[:2]] = found[2]
    return entries


def _prefixed_project(slug: str, names: tuple[str, ...]) -> str | None:
    matching = [name for name in names if slug == name or slug.startswith(f"{name}-")]
    return max(matching, key=len) if matching else None


def _proposed_note_project(
    note: Note, projects: _EntryProjects, logs: _DailyLogs
) -> tuple[str | None, str]:
    entries = _cited_entries(note, logs)
    votes = Counter(projects.of(entry) for entry in entries.values())
    ranked = votes.most_common(2)
    if ranked and ranked[0][0] is not None and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        return ranked[0][0], f"{ranked[0][1]} of {len(entries)} cited daily entries name it"
    if not entries:
        why = "no cited daily entry can be read"
    elif ranked and ranked[0][0] is None and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
        why = "most cited daily entries name no project"
    else:
        why = "the cited daily entries are split"
    prefixed = _prefixed_project(note.slug, projects.map.names())
    if prefixed is not None:
        return prefixed, f"{why}; the slug starts with its name"
    return None, f"{why}; no project name prefixes the slug"


def _notes_proposal(notes: list[Note], projects: _EntryProjects, logs: _DailyLogs) -> tuple[str, list[dict]]:
    lines = [NOTES_PROPOSAL_HEADER.rstrip("\n"), ""]
    rows = []
    for note in notes:
        if note.existing is not None:
            project, reason = note.existing, "already set in the note; kept"
        elif not note.has_frontmatter:
            project, reason = None, "the note has no frontmatter; left as it is"
        else:
            project, reason = _proposed_note_project(note, projects, logs)
        lines.append(f"- {note.slug}: {project or NO_PROJECT} — {reason}")
        rows.append({"note": note.slug, "project": project, "reason": reason})
    return "\n".join(lines) + "\n", rows


# --- propose ---------------------------------------------------------------------


def _operation_id(step: str) -> str:
    """Unique per run: after an undo, the same plan applies again as a new transaction."""
    return f"project-migration:{step}:{uuid.uuid4().hex}"


def _current_map(vault: Path) -> tuple[bytes | None, ProjectMap]:
    before = _read_optional(vault, MAP_RELATIVE_PATH)
    text = _decoded(before)
    return before, parse_project_map(text) if text is not None else ProjectMap((), ())


def propose(vault: Path, *, force: bool = False, write: bool = True) -> dict:
    """The proposals, written to the two private files unless `write` is false."""
    existing = [path for path in (MAP_PROPOSAL, NOTES_PROPOSAL) if (vault / path).exists()]
    if existing and write and not force:
        raise MigrationRefused(
            f"{', '.join(existing)} already exists; edit it and run the dry run, "
            "or pass --force to propose again"
        )
    before, current = _current_map(vault)
    resolve = _Resolver(vault)
    folders = old_folders(vault, set(current.names()), resolve)
    report: dict = {"step": "propose", "folders": _folder_rows(folders), "written": []}
    if not folders:
        report["message"] = "nothing to propose: no folder of the flat layout is left"
        return report
    entries = _proposed_entries(current, folders)
    map_text, _skipped = with_repositories(MAP_PROPOSAL_HEADER, entries)
    final_text, _skipped = with_repositories(_decoded(before), entries)
    final = parse_project_map(final_text)
    notes_text, rows = _notes_proposal(
        live_notes(vault), _EntryProjects(final, folders, resolve), _DailyLogs(vault)
    )
    report.update(
        {
            "registered": list(current.names()),
            "proposed": [
                {"project": name, "repository": repository.as_posix()}
                for name, repository in entries
            ],
            "notes": rows,
        }
    )
    if write:
        proposals = {MAP_PROPOSAL: map_text.encode("utf-8"), NOTES_PROPOSAL: notes_text.encode("utf-8")}
        record = mutate_knowledge(
            _operation_id("proposal"),
            {vault / path: content for path, content in proposals.items()},
        )
        report["written"] = sorted(proposals)
        report["transaction_id"] = record.id
    return report


def _folder_rows(folders: list[OldFolder]) -> list[dict]:
    return [
        {
            "folder": folder.name,
            "events": folder.events,
            "repository": folder.repository.as_posix() if folder.repository else None,
            "reason": folder.reason or None,
        }
        for folder in folders
    ]


# --- the plan --------------------------------------------------------------------


@dataclass
class Plan:
    map_before: bytes | None = None
    map_after: bytes | None = None
    final: ProjectMap = field(default_factory=lambda: ProjectMap((), ()))
    added: list[tuple[str, Path]] = field(default_factory=list)
    skipped: list[tuple[str, Path, str]] = field(default_factory=list)
    keeps: list[tuple[OldFolder, Placement]] = field(default_factory=list)
    deletes: list[tuple[OldFolder, str]] = field(default_factory=list)
    notes: list[tuple[Note, str]] = field(default_factory=list)
    notes_unchanged: list[tuple[str, str]] = field(default_factory=list)
    stale_keys: list[str] = field(default_factory=list)
    proposals: dict[str, bytes] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _parsed_notes_proposal(text: str) -> tuple[dict[str, str | None], list[str]]:
    assigned: dict[str, str | None] = {}
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _NOTE_LINE.match(line)
        if match is None:
            continue
        slug, value = match[1], match[2]
        if slug in assigned:
            problems.append(f"{NOTES_PROPOSAL}:{number}: note '{slug}' is listed twice")
            continue
        assigned[slug] = None if value == NO_PROJECT else value
    return assigned, problems


def _with_project(content: bytes, project: str) -> bytes:
    """The note with `project:` as the last frontmatter field; the rest byte for byte."""
    start = read_frontmatter(content).body_start
    head = content[:start]
    closing = head.rstrip(b"\r\n").rfind(b"\n") + 1
    newline = b"\r\n" if head[:closing].endswith(b"\r\n") else b"\n"
    escaped = project.replace("\\", "\\\\").replace('"', '\\"')
    line = f'project: "{escaped}"'.encode() + newline
    return content[:closing] + line + content[closing:]


def _plan_notes(vault: Path, plan: Plan, text: str) -> None:
    assigned, problems = _parsed_notes_proposal(text)
    plan.problems.extend(problems)
    notes = {note.slug: note for note in live_notes(vault)}
    for slug, project in assigned.items():
        note = notes.get(slug)
        if note is None:
            plan.problems.append(f"{NOTES_PROPOSAL}: no live note is named '{slug}'")
        elif project is None:
            continue
        elif note.existing is not None:
            if note.existing != project:
                plan.notes_unchanged.append((slug, f"already carries project '{note.existing}'"))
        elif not note.has_frontmatter:
            plan.notes_unchanged.append((slug, "has no frontmatter"))
        elif plan.final.project_named(project) is None:
            plan.problems.append(
                f"{NOTES_PROPOSAL}: note '{slug}' names '{project}', which the map will not register"
            )
        else:
            plan.notes.append((note, project))


def _plan_folders(vault: Path, plan: Plan, folders: list[OldFolder]) -> None:
    placed = {
        repository_key(placement.repository): placement
        for placement in placements(vault, project_map=plan.final)
    }
    groups: dict[str, list[OldFolder]] = {}
    for folder in folders:
        if folder.repository is None:
            plan.deletes.append((folder, folder.reason))
        elif plan.final.project_of(folder.repository) is None:
            plan.deletes.append((folder, "its repository is not in the project map"))
        else:
            groups.setdefault(repository_key(folder.repository), []).append(folder)
    destinations: dict[str, str] = {}
    for key, group in groups.items():
        placement = placed.get(key)
        if placement is None:
            names = ", ".join(folder.name for folder in group)
            plan.problems.append(f"the map entry for the repository of {names} cannot be placed")
            continue
        if (placement.directory(vault) / "journal.md").exists():
            for folder in group:
                plan.deletes.append(
                    (folder, f"its repository keeps its work state at {placement.relative}")
                )
            continue
        other = destinations.setdefault(placement.relative, key)
        if other != key:
            plan.problems.append(
                f"two repositories of project '{placement.project}' would share the folder "
                f"{placement.relative}; put one in another project, or attach it after the "
                "migration with manage_project"
            )
            continue
        kept = max(group, key=lambda folder: (folder.events, folder.name == placement.folder))
        plan.keeps.append((kept, placement))
        for folder in group:
            if folder is not kept:
                plan.deletes.append(
                    (folder, f"its repository keeps the folder with more events, {kept.name}")
                )
    plan.deletes.sort(key=lambda item: item[0].name)


def _pending_keys(state_root: Path) -> set[str]:
    try:
        state = json.loads((state_root / "run" / "state.json").read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    return _state_keys(state) if isinstance(state, dict) else set()


def _state_keys(state: dict) -> set[str]:
    keys: set[str] = set()
    for name in _STATE_QUEUES:
        if isinstance(state.get(name), dict):
            keys.update(state[name])
    if isinstance(state.get(_STATE_REDUCERS), dict):
        keys.update(key.split(":", 1)[0] for key in state[_STATE_REDUCERS])
    return keys


def _is_live_key(key: str, vault: Path, final: ProjectMap, moved: set[str]) -> bool:
    """Whether a registered repository's journal carries, or will first take, this key."""
    if key in moved:
        return True
    for placement in placements(vault, project_map=final):
        if recorded_key(placement.directory(vault)) == key:
            return True
        fresh = fresh_key(placement, lambda _key: False)
        if key == fresh or re.fullmatch(rf"{re.escape(fresh)}-\d+", key):
            return True
    return False


def plan_migration(vault: Path, state_root: Path) -> Plan:
    """What `--apply` would do with the proposals as they are now."""
    plan = Plan()
    map_proposal = _read_optional(vault, MAP_PROPOSAL)
    notes_proposal = _read_optional(vault, NOTES_PROPOSAL)
    plan.map_before, current = _current_map(vault)
    missing = [
        path
        for path, content in ((MAP_PROPOSAL, map_proposal), (NOTES_PROPOSAL, notes_proposal))
        if content is None
    ]
    if missing:
        plan.problems.append(f"missing {', '.join(missing)}: run --propose first")
        return plan
    plan.proposals = {MAP_PROPOSAL: map_proposal, NOTES_PROPOSAL: notes_proposal}
    proposal_text = map_proposal.decode("utf-8-sig")
    plan.problems.extend(
        f"{MAP_PROPOSAL}:{problem.line}: {problem.message}"
        + (f" ({problem.repository})" if problem.repository else "")
        for problem in map_text_problems(vault, proposal_text)
    )
    proposed = parse_project_map(proposal_text)
    entries = [(project.name, path) for project in proposed.projects for path in project.repositories]
    final_text, plan.skipped = with_repositories(_decoded(plan.map_before), entries)
    plan.map_after = final_text.encode("utf-8")
    plan.final = parse_project_map(final_text)
    plan.added = [
        (name, path)
        for name, path in entries
        if current.project_of(path) is None and plan.final.project_of(path) == name
    ]
    if plan.map_after == plan.map_before or not plan.added:
        plan.map_after = plan.map_before
    folders = old_folders(vault, set(plan.final.names()), _Resolver(vault))
    _plan_folders(vault, plan, folders)
    _plan_notes(vault, plan, notes_proposal.decode("utf-8-sig"))
    moved = {folder.key for folder, _placement in plan.keeps if folder.key}
    plan.stale_keys = sorted(
        key for key in _pending_keys(state_root) if not _is_live_key(key, vault, plan.final, moved)
    )
    return plan


# --- apply -----------------------------------------------------------------------


def _moved_files(vault: Path, folder: OldFolder, placement: Placement) -> dict[str, bytes]:
    """Each file at its new place; `state.md` generated again under the new folder."""
    destination = f"{PROJECTS}/{placement.relative}"
    moved = {f"{destination}/{Path(source).name}": _read(vault, source) for source in folder.files}
    journal = moved.get(f"{destination}/journal.md")
    if journal is not None and folder.key is not None:
        try:
            events = parse_journal_events(folder.key, journal)
        except (ValueError, RuntimeError):
            events = []
        if events:
            moved[f"{destination}/state.md"] = rendered_state(events, folder=placement.relative)
    return moved


def _changes(vault: Path, plan: Plan) -> tuple[dict[Path, bytes | None], dict[str, object], list[str]]:
    changes: dict[Path, bytes | None] = {}
    preconditions: dict[str, object] = {}

    def remove(relative: str, content: bytes) -> None:
        changes[vault / relative] = None
        preconditions[relative] = sha256_bytes(content)

    if plan.map_after != plan.map_before:
        changes[vault / MAP_RELATIVE_PATH] = plan.map_after
        preconditions[MAP_RELATIVE_PATH] = (
            ABSENT if plan.map_before is None else sha256_bytes(plan.map_before)
        )
    destinations = []
    for folder, placement in plan.keeps:
        destinations.append(f"{PROJECTS}/{placement.relative}")
        for source in folder.files:
            remove(source, _read(vault, source))
        for target, content in _moved_files(vault, folder, placement).items():
            existing = _read_optional(vault, target)
            changes[vault / target] = content
            preconditions[target] = ABSENT if existing is None else sha256_bytes(existing)
    for folder, _reason in plan.deletes:
        for source in folder.files:
            remove(source, _read(vault, source))
    for note, project in plan.notes:
        changes[vault / note.relative] = _with_project(note.content, project)
        preconditions[note.relative] = sha256_bytes(note.content)
    for relative, content in plan.proposals.items():
        remove(relative, content)
    return changes, preconditions, destinations


def _remove_leftovers(vault: Path, plan: Plan) -> int:
    """Atomic-write leftovers are not Markdown the transaction can own; nothing needs them."""
    removed = 0
    folders = [folder for folder, _placement in plan.keeps] + [folder for folder, _ in plan.deletes]
    for folder in folders:
        for relative in folder.leftovers:
            try:
                (vault / relative).unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _clear_stale_keys(stale: set[str]) -> None:
    def clear(state: dict) -> None:
        for name in _STATE_QUEUES:
            queues = state.get(name)
            if isinstance(queues, dict):
                for key in stale & set(queues):
                    del queues[key]
        reducers = state.get(_STATE_REDUCERS)
        if isinstance(reducers, dict):
            for key in [key for key in reducers if key.split(":", 1)[0] in stale]:
                del reducers[key]

    update_state(clear)


def apply_migration(vault: Path, state_root: Path, plan: Plan) -> dict:
    changes, preconditions, destinations = _changes(vault, plan)
    try:
        record = mutate_knowledge(
            _operation_id("apply"),
            changes,
            preconditions=preconditions,
        )
    except Exception:
        _prune_empty(vault, destinations)
        raise
    removed = _remove_leftovers(vault, plan)
    moved = {folder.key for folder, _placement in plan.keeps if folder.key}
    stale = {key for key in _pending_keys(state_root) if not _is_live_key(key, vault, plan.final, moved)}
    if stale:
        _clear_stale_keys(stale)
    # Issue #17: the project pages are regenerated here once they exist.
    return {"transaction_id": record.id, "leftovers_removed": removed, "keys_cleared": sorted(stale)}


# --- reporting -------------------------------------------------------------------


def _plan_payload(plan: Plan) -> dict:
    return {
        "map": {
            "path": MAP_RELATIVE_PATH,
            "changed": plan.map_after != plan.map_before,
            "projects": plan.final.as_data(),
            "added": [{"project": name, "repository": path.as_posix()} for name, path in plan.added],
            "kept_where_registered": [
                {"proposed": name, "repository": path.as_posix(), "registered_in": owner}
                for name, path, owner in plan.skipped
            ],
        },
        "keep": [
            {
                "folder": folder.name,
                "to": f"{PROJECTS}/{placement.relative}",
                "events": folder.events,
            }
            for folder, placement in plan.keeps
        ],
        "delete": [
            {"folder": folder.name, "events": folder.events, "reason": reason}
            for folder, reason in plan.deletes
        ],
        "notes": [{"note": note.slug, "project": project} for note, project in plan.notes],
        "notes_unchanged": [{"note": slug, "reason": why} for slug, why in plan.notes_unchanged],
        "checkpoint_keys_to_clear": plan.stale_keys,
        "problems": plan.problems,
    }


def _print_plan(payload: dict) -> None:
    mapped = payload["map"]
    added = {(item["project"], item["repository"]) for item in mapped["added"]}
    print(f"Project map ({mapped['path']}){'' if mapped['changed'] else ': unchanged'}")
    for project in mapped["projects"]:
        print(f"  {project['name']}")
        for repository in project["repositories"]:
            mark = "  (new)" if (project["name"], repository) in added else ""
            print(f"    - {repository}{mark}")
    for item in mapped["kept_where_registered"]:
        print(
            f"  note: {item['repository']} stays in '{item['registered_in']}', "
            f"not '{item['proposed']}'"
        )
    print(f"Old project folders: {len(payload['keep'])} kept, {len(payload['delete'])} deleted")
    for item in payload["keep"]:
        print(f"  KEEP    {item['folder']} -> {item['to']}/ ({item['events']} events)")
    for item in payload["delete"]:
        print(f"  DELETE  {item['folder']} ({item['events']} events): {item['reason']}")
    print(f"Notes that get a project: {len(payload['notes'])}")
    for item in payload["notes"]:
        print(f"  {item['note']}: {item['project']}")
    for item in payload["notes_unchanged"]:
        print(f"  unchanged: {item['note']} {item['reason']}")
    if payload["checkpoint_keys_to_clear"]:
        keys = ", ".join(payload["checkpoint_keys_to_clear"])
        print(f"Checkpoint queue keys to clear in run/state.json: {keys}")
    for problem in payload["problems"]:
        print(f"PROBLEM: {problem}")


def _print_deletions(payload: dict) -> None:
    print(
        f"These {len(payload['delete'])} folders will be deleted. The transaction can be undone "
        "for two days; after that the deletion is permanent:"
    )
    for item in payload["delete"]:
        print(f"  {PROJECTS}/{item['folder']}/")


def _print_proposal(report: dict) -> None:
    folders = report["folders"]
    resolved = [item for item in folders if item["repository"]]
    print(f"Old project folders: {len(folders)}, {len(resolved)} in a git repository")
    for item in folders:
        target = item["repository"] or f"none: {item['reason']}"
        print(f"  {item['folder']} ({item['events']} events) -> {target}")
    if "message" in report:
        print(report["message"])
        return
    print(f"Proposed projects: {len(report['proposed'])}")
    for item in report["proposed"]:
        print(f"  {item['project']}: {item['repository']}")
    assigned = [row for row in report["notes"] if row["project"]]
    print(f"Notes: {len(report['notes'])}, {len(assigned)} with a proposed project")
    for path in report["written"]:
        print(f"wrote {path}")
    if report["written"]:
        print("Edit both files, then run the dry run (no flag) and --apply.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    step = parser.add_mutually_exclusive_group()
    step.add_argument("--propose", action="store_true", help="write the two proposals")
    step.add_argument("--apply", action="store_true", help="apply the approved proposals")
    parser.add_argument("--force", action="store_true", help="with --propose: replace existing proposals")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    arguments = parser.parse_args(argv)
    _utf8_stdout()
    vault, state_root = Path(ROOT), Path(STATE_ROOT)
    if arguments.propose:
        return _run_propose(vault, arguments)
    return _run_plan(vault, state_root, arguments)


def _run_propose(vault: Path, arguments: argparse.Namespace) -> int:
    try:
        report = propose(vault, force=arguments.force)
    except (MigrationRefused, OSError, RuntimeError, ValueError) as error:
        return _refused(arguments, "propose", error)
    if arguments.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_proposal(report)
    return 0


def _nothing_left(vault: Path) -> bool:
    """No flat-layout folder is left: the migration already happened."""
    names = set(_current_map(vault)[1].names())
    return not old_folders(vault, names, _Resolver(vault))


def _run_plan(vault: Path, state_root: Path, arguments: argparse.Namespace) -> int:
    if arguments.apply:
        _prune_expired_empty(vault)
    try:
        plan = plan_migration(vault, state_root)
    except (OSError, RuntimeError, ValueError) as error:
        return _refused(arguments, "plan", error)
    if not plan.proposals:
        if _nothing_left(vault):
            return _report(arguments, {"applied": False, "message": "nothing to migrate"}, 0)
        return _report(arguments, {"applied": False, "problems": plan.problems}, 1)
    payload = {"applied": False, **_plan_payload(plan)}
    if not arguments.json:
        _print_plan(payload)
    if plan.problems:
        if arguments.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print("nothing was written: fix the problems above first")
        return 1
    if not arguments.apply:
        if arguments.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            _print_deletions(payload)
            print("dry run: nothing was written; rerun with --apply to write")
        return 0
    if not arguments.json:
        _print_deletions(payload)
    try:
        payload.update(apply_migration(vault, state_root, plan), applied=True)
    except (OSError, RuntimeError, ValueError) as error:
        return _refused(arguments, "apply", error)
    if arguments.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"transaction: {payload['transaction_id']}")
        print(f"undo within two days: uv run python scripts/markdown_transaction.py undo {payload['transaction_id']}")
        if payload["keys_cleared"]:
            print(f"cleared checkpoint queue keys: {', '.join(payload['keys_cleared'])}")
    return 0


def _report(arguments: argparse.Namespace, payload: dict, status: int) -> int:
    if arguments.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for problem in payload.get("problems", []):
            print(f"PROBLEM: {problem}")
        if "message" in payload:
            print(payload["message"])
    return status


def _refused(arguments: argparse.Namespace, step: str, error: Exception) -> int:
    message = f"{type(error).__name__}: {error}"
    if arguments.json:
        print(json.dumps({"step": step, "error": message}, indent=2, ensure_ascii=False))
    else:
        print(f"ERROR ({step}): {message}")
    return 1


def _utf8_stdout() -> None:
    """Reasons carry an em dash, which Windows cp1252 cannot print."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
