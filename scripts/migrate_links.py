"""Migrate the links existing notes carry to the form Obsidian opens (ADR 0003).

The vault is read in Obsidian rooted at `knowledge/`, and lint resolves links the
same way: a bare `[[slug]]` names a file anywhere in the vault, a link with a slash
is a path from `knowledge/`. Pages written before that rule carry two leftovers:

- backlink lines the retired repair pass wrote, `- [[knowledge/notes/x]] — links to
  this page.`, which Obsidian's backlinks pane already shows. They are removed, and
  a `## Related` heading their removal leaves empty goes with them.
- repository-rooted links, `[[knowledge/notes/x]]`, which no longer resolve. A note
  becomes a bare `[[x]]` and any other file a vault-relative `[[projects/a/page]]`;
  alias and heading are kept. A link is rewritten only when its new form opens the
  very file the old one named.

Every link that still resolves to nothing is reported and left as it is: no guess
is ever written. The `## Claims` ledger, `## Evidence` lines, code fences and
inline code are never rewritten.

Scope: Markdown under `knowledge/notes`, `knowledge/projects`, `knowledge/inbox`
and `knowledge/feedback`. Daily logs and `knowledge/raw/` are evidence and are not
touched; editorial pages (READMEs, `state.md`, indexes, logs) and project journals
belong to their own writers.

A dry run lists every change and writes nothing. `--apply` writes every changed
page in one recoverable transaction, so `markdown_transaction.py undo <id>` reverts
the whole migration inside the 2-day undo window. A second apply changes nothing.
A page that cannot be read is reported and skipped, and the exit status is 1.

Usage:
    uv run python scripts/migrate_links.py            # dry run
    uv run python scripts/migrate_links.py --apply
    uv run python scripts/migrate_links.py --json     # machine-readable report
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES, read_stable_bytes  # noqa: E402
from claims import CLAIM_LEDGER_RE  # noqa: E402
from lint_memory import VAULT, is_placeholder_target, resolve_link  # noqa: E402
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import canonical_json_bytes, sha256_bytes  # noqa: E402
from vault_editorial import EDITORIAL_NAMES, editorial_parents_to_skip  # noqa: E402

SCOPE_DIRECTORIES = ("notes", "projects", "inbox", "feedback")
# The append-only project journal is written only through its fenced checkpoints.
SKIP_NAMES = EDITORIAL_NAMES | {"journal.md"}
REPOSITORY_PREFIX = "knowledge/"
# The headings the retired repair pass wrote its lines under.
RELATED_HEADINGS = ("## Related", "## Links", "## Related pages")
PROTECTED_SECTIONS = ("## Claims", "## Evidence")

# The repair pass wrote an em dash; hand-copied variants use a hyphen or two.
BACKLINK_LINE_RE = re.compile(
    r"^\s*[-*+]\s+\[\[[^\[\]\r\n]+\]\]\s*(?:—|–|--|-)\s*links to this page\.?\s*$",
    re.IGNORECASE,
)
# target, then an optional table-escaped pipe, then `#heading` and/or `|alias`.
LINK_RE = re.compile(
    r"\[\[(?P<target>[^\[\]|#\r\n]*?)(?P<escape>\\?)(?P<rest>[#|][^\[\]\r\n]*)?\]\]"
)
CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
SECTION_RE = re.compile(r"^#{1,2}\s")


@dataclass
class PagePlan:
    path: Path
    before: bytes
    after: bytes
    changes: list[dict[str, object]] = field(default_factory=list)
    unresolved: list[dict[str, object]] = field(default_factory=list)

    @property
    def relative(self) -> str:
        return self.path.relative_to(ROOT).as_posix()


@dataclass
class MigrationReport:
    pages: list[PagePlan] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    scanned: int = 0
    transaction_id: str | None = None

    @property
    def changed(self) -> list[PagePlan]:
        return [page for page in self.pages if page.after != page.before]


class _Resolver:
    """lint's resolver, asked once per distinct target."""

    def __init__(self) -> None:
        self._seen: dict[str, Path | None] = {}

    def __call__(self, target: str) -> Path | None:
        if target not in self._seen:
            found = resolve_link(target)
            self._seen[target] = found.resolve() if found is not None else None
        return self._seen[target]


def _repository_rooted_file(target: str) -> Path | None:
    """The file `[[knowledge/...]]` named when links were read from the repository root."""
    for candidate in (ROOT / f"{target}.md", ROOT / target):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(VAULT.resolve())
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _migrated_target(target: str, resolve: _Resolver) -> str | None:
    """The vault form of a repository-rooted target, or None to leave it alone."""
    if not target.startswith(REPOSITORY_PREFIX):
        return None
    named = _repository_rooted_file(target)
    if named is None:
        return None
    relative = target[len(REPOSITORY_PREFIX):]
    if relative.endswith(".md"):
        relative = relative[: -len(".md")]
    candidates = [relative]
    if relative.startswith("notes/"):
        candidates.insert(0, PurePosixPath(relative).name)
    for candidate in candidates:
        if resolve(candidate) == named:
            return candidate
    return None


def _is_link_to_check(target: str) -> bool:
    return bool(target) and not is_placeholder_target(target)


class _PageMigration:
    """One page's pass: which lines go, which links change, which stay broken."""

    def __init__(self, text: str, resolve: _Resolver) -> None:
        self.lines = text.splitlines(keepends=True)
        self.resolve = resolve
        self.kept: list[tuple[int, str]] = []
        self.changes: list[dict[str, object]] = []
        self.unresolved: list[dict[str, object]] = []
        self.emptied_headings: set[int] = set()

    def run(self) -> str:
        for number, line, kind, heading in self._classified():
            self._line(number, line, kind, heading)
        return "".join(self._without_emptied_headings())

    def _classified(self):
        """(line number, line, region, index of the governing heading)."""
        fence: str | None = None
        section = ""
        heading = -1
        for index, line in enumerate(self.lines):
            bare = line.rstrip("\r\n")
            fence, in_fence = _fence_state(fence, bare)
            if not in_fence and SECTION_RE.match(bare):
                section, heading = bare.rstrip(), index
            yield index + 1, line, _region(in_fence, section, bare), heading

    def _line(self, number: int, line: str, kind: str, heading: int) -> None:
        bare = line.rstrip("\r\n")
        if kind == "prose" and BACKLINK_LINE_RE.match(bare):
            self.changes.append({"line": number, "action": "remove_backlink", "text": bare})
            if heading >= 0 and self.lines[heading].rstrip() in RELATED_HEADINGS:
                self.emptied_headings.add(heading)
            return
        if kind == "prose":
            line = self._rewritten(number, line)
        if kind in ("prose", "evidence"):
            self._report_unresolved(number, line)
        self.kept.append((number - 1, line))

    def _rewritten(self, number: int, line: str) -> str:
        pieces = []
        cursor = 0
        for span in CODE_SPAN_RE.finditer(line):
            pieces.append(self._rewritten_links(number, line[cursor : span.start()]))
            pieces.append(span.group(0))
            cursor = span.end()
        pieces.append(self._rewritten_links(number, line[cursor:]))
        return "".join(pieces)

    def _rewritten_links(self, number: int, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            new_target = _migrated_target(match["target"].strip(), self.resolve)
            if new_target is None:
                return match.group(0)
            new = f"[[{new_target}{match['escape']}{match['rest'] or ''}]]"
            self.changes.append(
                {"line": number, "action": "rewrite_link", "from": match.group(0), "to": new}
            )
            return new

        return LINK_RE.sub(replace, text)

    def _report_unresolved(self, number: int, line: str) -> None:
        prose = CODE_SPAN_RE.sub("", line)
        for match in LINK_RE.finditer(prose):
            target = match["target"].strip()
            if _is_link_to_check(target) and self.resolve(target) is None:
                self.unresolved.append({"line": number, "link": match.group(0)})

    def _without_emptied_headings(self) -> list[str]:
        """Drop a Related heading whose only entries were backlink lines."""
        kept = dict(self.kept)
        order = [index for index, _line in self.kept]
        dropped: set[int] = set()
        for heading in sorted(self.emptied_headings):
            body = _section_body(order, heading, self.lines)
            if all(not kept[index].strip() for index in body):
                dropped.update({heading, *body})
                self.changes.append(
                    {
                        "line": heading + 1,
                        "action": "remove_empty_heading",
                        "text": self.lines[heading].rstrip("\r\n"),
                    }
                )
        result = [kept[index] for index in order if index not in dropped]
        if dropped and max(dropped) == order[-1]:
            while result and not result[-1].strip():
                result.pop()
        return result


def _fence_state(fence: str | None, bare: str) -> tuple[str | None, bool]:
    """The open fence after this line, and whether this line is inside one."""
    match = FENCE_RE.match(bare)
    if fence is None:
        return (match.group(1), True) if match else (None, False)
    if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
        if not bare.strip()[len(match.group(1)):].strip():
            return None, True
    return fence, True


def _region(in_fence: bool, section: str, bare: str) -> str:
    if in_fence:
        return "code"
    if section == "## Claims":
        return "claims"
    if section == "## Evidence" and not SECTION_RE.match(bare):
        return "evidence"
    return "prose"


def _section_body(order: list[int], heading: int, lines: list[str]) -> list[int]:
    """The kept line indexes after a heading, up to the next section heading."""
    body = []
    for index in order:
        if index <= heading:
            continue
        if SECTION_RE.match(lines[index]):
            break
        body.append(index)
    return body


def _ledgers(content: bytes) -> list[bytes]:
    return [match.group(0) for match in CLAIM_LEDGER_RE.finditer(content)]


def plan_page(path: Path, before: bytes, resolve: _Resolver) -> PagePlan:
    migration = _PageMigration(before.decode("utf-8"), resolve)
    after = migration.run().encode("utf-8")
    if _ledgers(after) != _ledgers(before):
        raise ValueError("the migration would change the Claims ledger")
    return PagePlan(
        path,
        before,
        after,
        sorted(migration.changes, key=lambda change: int(change["line"])),
        migration.unresolved,
    )


def _skipped(path: Path, skipped_parents: tuple[Path, ...]) -> bool:
    if path.name in SKIP_NAMES or not path.is_file() or path.is_symlink():
        return True
    return any(parent.resolve() in skipped_parents for parent in path.parents)


def candidate_pages() -> list[Path]:
    skipped_parents = editorial_parents_to_skip(VAULT)
    pages: list[Path] = []
    for name in SCOPE_DIRECTORIES:
        root = VAULT / name
        if root.is_dir():
            pages.extend(
                page for page in sorted(root.rglob("*.md")) if not _skipped(page, skipped_parents)
            )
    return pages


def plan_migration() -> MigrationReport:
    report = MigrationReport()
    resolve = _Resolver()
    for path in candidate_pages():
        report.scanned += 1
        try:
            before = read_stable_bytes(path, MAX_KNOWLEDGE_PAGE_BYTES, label="link migration page")
            report.pages.append(plan_page(path, before, resolve))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            report.errors.append(
                {"path": path.relative_to(ROOT).as_posix(), "error": f"{type(exc).__name__}: {exc}"}
            )
    return report


def apply_migration(report: MigrationReport) -> None:
    """Every changed page in one transaction, each guarded by the bytes it was planned from."""
    changed = report.changed
    if not changed:
        return
    identity = canonical_json_bytes(
        [[page.relative, sha256_bytes(page.before), sha256_bytes(page.after)] for page in changed]
    )
    record = mutate_knowledge(
        stable_operation_id("link-migration", "knowledge", identity),
        {page.path: page.after for page in changed},
        preconditions={page.relative: sha256_bytes(page.before) for page in changed},
    )
    report.transaction_id = record.id


def _payload(report: MigrationReport, applied: bool) -> dict[str, object]:
    return {
        "applied": applied,
        "transaction_id": report.transaction_id,
        "scanned": report.scanned,
        "changed": len(report.changed),
        "pages": [
            {"path": page.relative, "changes": page.changes, "unresolved": page.unresolved}
            for page in report.pages
            if page.changes or page.unresolved
        ],
        "errors": report.errors,
    }


def _change_line(change: dict[str, object]) -> str:
    if change["action"] == "rewrite_link":
        return f"line {change['line']}: REWRITE {change['from']} -> {change['to']}"
    if change["action"] == "remove_backlink":
        return f"line {change['line']}: REMOVE BACKLINK {change['text']}"
    return f"line {change['line']}: REMOVE EMPTY HEADING {change['text']}"


def _print_plain(payload: dict[str, object]) -> None:
    for page in payload["pages"]:
        print(page["path"])
        for change in page["changes"]:
            print(f"  {_change_line(change)}")
        for item in page["unresolved"]:
            print(f"  line {item['line']}: UNRESOLVED {item['link']} (left as it is)")
    for error in payload["errors"]:
        print(f"ERROR: {error['path']}: {error['error']}")
    unresolved = sum(len(page["unresolved"]) for page in payload["pages"])
    verb = "changed" if payload["applied"] else "would change"
    print(
        f"migrate_links: {payload['scanned']} pages scanned, {payload['changed']} {verb}, "
        f"{unresolved} unresolved links, {len(payload['errors'])} errors"
    )
    if payload["transaction_id"]:
        print(f"transaction: {payload['transaction_id']}")
    elif not payload["applied"] and payload["changed"]:
        print("dry run: nothing was written; rerun with --apply to write")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    arguments = parser.parse_args()
    _utf8_stdout()
    report = plan_migration()
    applied = arguments.apply
    if applied:
        try:
            apply_migration(report)
        except (OSError, RuntimeError, ValueError) as exc:
            report.errors.append({"path": "knowledge", "error": f"{type(exc).__name__}: {exc}"})
            applied = False
    payload = _payload(report, applied)
    if arguments.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        _print_plain(payload)
    return 1 if report.errors else 0


def _utf8_stdout() -> None:
    """Backlink lines carry an em dash, which Windows cp1252 cannot print."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
