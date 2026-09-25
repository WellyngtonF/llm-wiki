"""Module tags on notes: what a tag is, and how a note's `tags:` grows.

Stage 3 of `docs/specs/2026-09-24-readable-memory.md`, issue #22. A tag names a
module, an area inside a repository, never the project itself (`CONTEXT.md`). The
compile's model proposes tags; this module decides what is kept:

- a tag is lowercase kebab-case of at most `MAX_TAG_CHARS`; anything else is
  folded into that form, and dropped when it cannot be;
- the project's own name is never a tag of its notes;
- one operation keeps at most `MAX_PROPOSED_TAGS`, and a note stops gaining tags
  at `MAX_NOTE_TAGS`. A tag already on a note is never removed.

`tags:` is written as a YAML flow list, `tags: [backend, queue]`, the form
hand-written notes use; every reader parses it through `yaml.safe_load`.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

import yaml
from corpus_snapshot import read_frontmatter

MAX_TAG_CHARS = 40
MAX_PROPOSED_TAGS = 5
MAX_NOTE_TAGS = 8
TAG_PATTERN = "^[a-z0-9]+(?:-[a-z0-9]+)*$"

_NOT_KEBAB = re.compile(r"[^a-z0-9]+")
_TAGS_KEY = re.compile(r"^tags[ \t]*:")
# The lines that continue a top-level value: indented text or block-list items.
_CONTINUATION = re.compile(r"^(?:[ \t]+\S|-(?:[ \t]|\r?$))")


def normalized_tag(value: object) -> str | None:
    """`Backend Lease` and `backend_lease` are both `backend-lease`; a non-string is none."""
    if not isinstance(value, str):
        return None
    tag = _NOT_KEBAB.sub("-", value.casefold()).strip("-")
    if not tag or len(tag) > MAX_TAG_CHARS:
        return None
    return tag


def proposed_tags(values: Iterable[object], project: str | None) -> list[str]:
    """The proposed tags a note may carry: normalised, deduped, capped, never its project."""
    own_name = normalized_tag(project) if project else None
    kept: list[str] = []
    for value in values:
        tag = normalized_tag(value)
        if tag is None or tag == own_name or tag in kept:
            continue
        kept.append(tag)
    return kept[:MAX_PROPOSED_TAGS]


def page_tags(content: bytes) -> list[str]:
    """A note's tags in normalised form; an unreadable `tags:` is no tags."""
    value = read_frontmatter(content).mapping.get("tags")
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list):
        return []
    tags = (normalized_tag(item) for item in items)
    return list(dict.fromkeys(tag for tag in tags if tag))


def tags_line(tags: Sequence[object]) -> str:
    """`tags: [a, b]\\n`; YAML quotes what it would otherwise read as another type."""
    rendered = yaml.safe_dump(
        list(tags), default_flow_style=True, width=1 << 20, allow_unicode=True
    )
    return f"tags: {rendered.strip()}\n"


def with_tags(page: bytes, tags: Sequence[str]) -> tuple[bytes, list[str]]:
    """The page with `tags` appended to its `tags:`, and the tags actually added.

    Existing tags keep their order and spelling. A page without frontmatter, with
    frontmatter that does not parse, or with a `tags:` that is not a list or a
    comma-separated string is left as it is: rewriting what cannot be read back
    could lose what the owner wrote. The rewrite is checked by parsing it again;
    only `tags` may differ.
    """
    frontmatter = read_frontmatter(page)
    existing = frontmatter.mapping.get("tags")
    if frontmatter.body_start == 0 or frontmatter.problem is not None:
        return page, []
    current = _existing_items(existing)
    if current is None:
        return page, []
    present = {normalized_tag(item) for item in current}
    room = max(0, MAX_NOTE_TAGS - len(current))
    added = [tag for tag in tags if tag not in present][:room]
    if not added:
        return page, []
    rewritten = _rewritten_frontmatter(page, frontmatter.body_start, [*current, *added])
    after = read_frontmatter(rewritten)
    expected = {**frontmatter.mapping, "tags": [*current, *added]}
    if after.problem is not None or after.mapping != expected:
        return page, []
    return rewritten, added


def _existing_items(value: object) -> list[object] | None:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return list(value)
    return None


def _rewritten_frontmatter(page: bytes, body_start: int, tags: Sequence[object]) -> bytes:
    """Replace the `tags:` key and its continuation lines, or add it before the close."""
    lines = page[:body_start].decode("utf-8").splitlines(keepends=True)
    opening, inner, closing = lines[0], lines[1:-1], lines[-1]
    newline = "\r\n" if opening.endswith("\r\n") else "\n"
    line = tags_line(tags).replace("\n", newline)
    start = next((index for index, text in enumerate(inner) if _TAGS_KEY.match(text)), None)
    if start is None:
        inner = [*inner, line]
    else:
        end = start + 1
        while end < len(inner) and _CONTINUATION.match(inner[end]):
            end += 1
        inner = [*inner[:start], line, *inner[end:]]
    return "".join([opening, *inner, closing]).encode("utf-8") + page[body_start:]
