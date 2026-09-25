"""The project a note belongs to: derived for a new note, renamed with its project.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002. The model never
chooses a note's project; the compile derives it from where the cited work
happened, through the project map as it is at compile time:

- a capture block or session-end entry names its repository's main checkout in a
  `- Repository:` line; the map says which project owns that checkout now, so a
  repository moved to another project files its new notes there. The entry's own
  `- Project:` line is what the map said when the session ran, and is not read;
- a prompt or tool breadcrumb names `<project>/<repository>` in its tag, and counts
  only while that is still where a registered repository's work state lives;
- anything else, including work in no registered repository, names no project.

Each distinct cited entry is one vote, and "no project" is a candidate like any
other. The candidate with most votes wins; a tie for the most gives no project, so
a note is never filed under a project its evidence does not clearly point to.

Renaming a project renames the `project:` of every note that names it
(`with_renamed_project`), so its notes stay on its page.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import dropwhile, takewhile
from pathlib import Path

from corpus_snapshot import read_frontmatter
from evidence_resolver import daily_entries
from project_map import ProjectMap, ProjectMapError, read_project_map
from work_state import placements

_METADATA = re.compile(r"^- ([A-Za-z][A-Za-z ]*): `([^`\r\n]*)`$")
_BREADCRUMB = re.compile(
    r"^[-+*]\s+`\[(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\] (prompt|tool) \| ([^`\r\n]*)`"
)
# A breadcrumb's fields after its kind, and which of them is the tag:
# `prompt | <session> | <tag>` and `tool | <agent> | <session> | <tag> | <tool>`.
_TAG_FIELD = {"prompt": (2, 1), "tool": (4, 2)}
_PROJECT_KEY = re.compile(r"^project[ \t]*:")
# The lines that continue a top-level value: indented text or block-list items.
_CONTINUATION = re.compile(r"^(?:[ \t]+\S|-(?:[ \t]|\r?$))")

Citation = tuple[str, bytes, int]
"""One cited evidence span: its source's identity, the source's bytes, its offset."""


@dataclass(frozen=True)
class NoteProjects:
    project_map: ProjectMap
    tags: Mapping[str, str]

    @classmethod
    def of_vault(cls, vault: Path) -> NoteProjects:
        """The map as it is now; an unreadable map registers nothing (doctor names it)."""
        try:
            project_map = read_project_map(vault)
        except (OSError, ProjectMapError):
            project_map = ProjectMap((), ())
        tags = {
            placement.relative: placement.project
            for placement in placements(vault, project_map=project_map)
        }
        return cls(project_map, tags)

    def of_citations(self, citations: Iterable[Citation]) -> str | None:
        """The project most of the distinct cited entries name, or None."""
        entries: dict[tuple[str, int], bytes] = {}
        for source, content, offset in citations:
            for _block, start, end in daily_entries(content):
                if start <= offset < end:
                    entries[(source, start)] = content[start:end]
                    break
        votes = Counter(self.of_entry(entry) for entry in entries.values())
        ranked = votes.most_common(2)
        if not ranked or (len(ranked) == 2 and ranked[0][1] == ranked[1][1]):
            return None
        return ranked[0][0]

    def of_entry(self, entry: bytes) -> str | None:
        """The project one daily entry's work belongs to, or None."""
        lines = [line.strip() for line in entry.decode("utf-8", "replace").splitlines()[1:]]
        body = list(dropwhile(lambda line: not line, lines))
        crumb = _BREADCRUMB.match(body[0]) if body else None
        if crumb is not None:
            return self._tag_project(crumb[1], crumb[2])
        for field in takewhile(bool, (_METADATA.match(line) for line in body)):
            if field[1] == "Repository":
                return self.project_map.project_of(field[2])
        return None

    def _tag_project(self, kind: str, fields: str) -> str | None:
        parts = fields.split(" | ")
        count, index = _TAG_FIELD[kind]
        if len(parts) != count:
            return None
        return self.tags.get(parts[index].strip())


def with_renamed_project(content: bytes, project: str) -> bytes | None:
    """The note with its `project:` value replaced by `project`; the rest byte for byte.

    None when the note has no `project:` or its frontmatter does not read back with
    only that value changed: rewriting what cannot be read back could lose what the
    owner wrote.
    """
    frontmatter = read_frontmatter(content)
    if frontmatter.body_start == 0 or frontmatter.problem is not None:
        return None
    lines = content[: frontmatter.body_start].decode("utf-8").splitlines(keepends=True)
    start = next((index for index, text in enumerate(lines) if _PROJECT_KEY.match(text)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) - 1 and _CONTINUATION.match(lines[end]):
        end += 1
    newline = "\r\n" if lines[start].endswith("\r\n") else "\n"
    escaped = project.replace("\\", "\\\\").replace('"', '\\"')
    lines[start:end] = [f'project: "{escaped}"{newline}']
    rewritten = "".join(lines).encode("utf-8") + content[frontmatter.body_start :]
    after = read_frontmatter(rewritten)
    if after.problem is not None or after.mapping != {**frontmatter.mapping, "project": project}:
        return None
    return rewritten
