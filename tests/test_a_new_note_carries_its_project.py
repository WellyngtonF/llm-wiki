"""A compiled note carries the project of the work it came from, never the model's pick.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002, issue #16. The
compile resolves `project:` from the daily entries a new note's evidence cites: a
capture or session-end entry's `- Repository:` line, or a breadcrumb's
`<project>/<repository>` tag, looked up in the project map as it is now. Work in
no registered repository gives no project; evidence split between projects gives
the project most cited entries name, and a tie gives none. An update never
touches the existing note's `project:`.

Each test runs the compile command over a temporary vault with the fake provider
and reads the note a person would open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPILE = ROOT / "scripts" / "compile_memory.py"
DAY = "2026-09-20"
SLUG = "retry-needs-deadline"
NOTE = Path("knowledge/notes") / f"{SLUG}.md"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    for relative in ("knowledge/daily", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    (tmp_path / "state" / "run").mkdir(parents=True)
    (root / "knowledge/index.md").write_text("# Index\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("contract\n", encoding="utf-8")
    return root


def _checkout(vault: Path, name: str) -> str:
    return (vault.parent / "work" / name).as_posix()


def _map(vault: Path, projects: dict[str, list[str]]) -> None:
    sections = "".join(
        f"\n## {name}\n\n" + "".join(f"- {path}\n" for path in paths)
        for name, paths in projects.items()
    )
    (vault / "knowledge/projects/project-map.md").write_text(
        f"# Project map\n{sections}", encoding="utf-8"
    )


def _capture(time: str, lesson: str, *, project: str = "", repository: str = "") -> str:
    location = ""
    if project:
        location = f"- Project: `{project}`\n- Repository: `{repository}`\n"
    return (
        f"\n## [{time}] session-end | session-{time[:2]}{time[3:5]}\n"
        "- Trigger: `session-end`\n"
        "- Agent: `claude`\n"
        f"{location}"
        "- Tier: `durable`\n\n"
        f"{lesson}\n"
    )


def _breadcrumb(time: str, tag: str, prompt: str) -> tuple[str, str]:
    line = f"`[{time}] prompt | abcdef12 | {tag}` {prompt}"
    return f"\n<!-- llm-wiki-operation:{'ab' * 16} -->\n- {line}\n", line


def _daily(vault: Path, *entries: str) -> None:
    (vault / "knowledge/daily" / f"{DAY}.md").write_text(
        f"# Daily log {DAY}\n" + "".join(entries), encoding="utf-8"
    )


def _reply(action: str, citations: list[tuple[str, str]]) -> str:
    operation = {
        "action": action,
        "category": "patterns",
        "slug": SLUG,
        "title": "Retry needs a deadline",
        "summary": "A retry loop needs a deadline.",
        "body_section": "Lesson",
        "body_markdown": "Bound every retry loop by a deadline.",
        "evidence": [
            {
                "daily_date": DAY,
                "timestamp": time,
                "quoted_text": quote,
                "claim": "A retry loop needs a deadline.",
            }
            for time, quote in citations
        ],
        "related": [],
    }
    draft = {"operations": [operation], "audit": {}}
    review = {"reviews": [{"slug": SLUG, "verdict": "pass", "reason": "ok"}]}
    return json.dumps(draft) + "\n" + json.dumps(review)


def _compile(vault: Path, reply: str) -> str:
    environment = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(vault.parent / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
        "MEMORY_LLM_FAKE_RESPONSE": reply,
    }
    done = subprocess.run(
        [sys.executable, str(COMPILE)],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return (vault / NOTE).read_text(encoding="utf-8")


def _frontmatter(note: str) -> list[str]:
    return note.split("---\n")[1].splitlines()


def _project_lines(note: str) -> list[str]:
    return [line for line in _frontmatter(note) if line.startswith("project:")]


LESSON = "A retry loop needs a deadline or it spins forever."
OTHER = "A retry without a deadline held the queue for an hour."
THIRD = "Every retry loop in the worker now stops at its deadline."


def test_a_note_from_a_registered_repository_carries_its_project(vault):
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend]})
    _daily(vault, _capture("10:00:00", LESSON, project="product-a", repository=backend))

    note = _compile(vault, _reply("create", [("10:00:00", LESSON)]))

    frontmatter = _frontmatter(note)
    assert 'project: "product-a"' in frontmatter
    assert frontmatter.index('project: "product-a"') == 2  # after type: and title:


def test_a_note_from_unregistered_work_carries_no_project(vault):
    _map(vault, {"product-a": [_checkout(vault, "backend")]})
    _daily(vault, _capture("10:00:00", LESSON))

    note = _compile(vault, _reply("create", [("10:00:00", LESSON)]))

    assert _project_lines(note) == []


def test_the_project_is_read_from_the_map_as_it_is_now(vault):
    """A repository moved to another project since the session files its note there."""
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [], "product-b": [backend]})
    _daily(vault, _capture("10:00:00", LESSON, project="product-a", repository=backend))

    note = _compile(vault, _reply("create", [("10:00:00", LESSON)]))

    assert _project_lines(note) == ['project: "product-b"']


def test_a_repository_no_longer_in_the_map_gives_no_project(vault):
    """The entry's own `Project:` line is history, not a registration."""
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [_checkout(vault, "frontend")]})
    _daily(vault, _capture("10:00:00", LESSON, project="product-a", repository=backend))

    note = _compile(vault, _reply("create", [("10:00:00", LESSON)]))

    assert _project_lines(note) == []


def test_a_breadcrumb_names_its_project_through_the_map(vault):
    _map(vault, {"product-a": [_checkout(vault, "backend")]})
    entry, line = _breadcrumb("10:05:00", "product-a/backend", LESSON)
    _daily(vault, entry)

    note = _compile(vault, _reply("create", [("10:05:00", line)]))

    assert _project_lines(note) == ['project: "product-a"']


def test_evidence_from_several_projects_goes_to_the_one_most_entries_name(vault):
    backend, web = _checkout(vault, "backend"), _checkout(vault, "web")
    _map(vault, {"product-a": [backend], "product-b": [web]})
    _daily(
        vault,
        _capture("10:00:00", LESSON, project="product-a", repository=backend),
        _capture("11:00:00", OTHER, project="product-b", repository=web),
        _capture("12:00:00", THIRD, project="product-a", repository=backend),
    )

    note = _compile(
        vault,
        _reply("create", [("10:00:00", LESSON), ("11:00:00", OTHER), ("12:00:00", THIRD)]),
    )

    assert _project_lines(note) == ['project: "product-a"']


def test_evidence_split_evenly_between_projects_gives_no_project(vault):
    backend, web = _checkout(vault, "backend"), _checkout(vault, "web")
    _map(vault, {"product-a": [backend], "product-b": [web]})
    _daily(
        vault,
        _capture("10:00:00", LESSON, project="product-a", repository=backend),
        _capture("11:00:00", OTHER, project="product-b", repository=web),
    )

    note = _compile(vault, _reply("create", [("10:00:00", LESSON), ("11:00:00", OTHER)]))

    assert _project_lines(note) == []


def test_unregistered_work_counts_as_its_own_side_of_a_tie(vault):
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend]})
    _daily(
        vault,
        _capture("10:00:00", LESSON, project="product-a", repository=backend),
        _capture("11:00:00", OTHER),
    )

    note = _compile(vault, _reply("create", [("10:00:00", LESSON), ("11:00:00", OTHER)]))

    assert _project_lines(note) == []


@pytest.mark.parametrize("existing", ['project: "product-b"', None])
def test_an_update_leaves_the_notes_project_as_it_was(vault, existing):
    """Neither replaced nor added: backfilling old notes is the owner-approved migration."""
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend], "product-b": []})
    _daily(vault, _capture("10:00:00", LESSON, project="product-a", repository=backend))
    header = ["type: pattern", 'title: "Retry needs a deadline"']
    if existing:
        header.append(existing)
    (vault / NOTE).write_text(
        "---\n" + "\n".join(header) + "\n---\n\n# Retry needs a deadline\n\n"
        "One-sentence summary: A retry loop needs a deadline.\n",
        encoding="utf-8",
    )

    note = _compile(vault, _reply("update", [("10:00:00", LESSON)]))

    assert "## Update (" in note
    assert _project_lines(note) == ([existing] if existing else [])
