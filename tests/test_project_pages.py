"""Each project has a generated page, and notes without one are on the General page.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002 and ADR 0003,
issue #17. `knowledge/projects/<project>/index.md` lists the project's live notes
by type, each as a bare link and its one-sentence summary, the module tags they
use, and each repository's work state. `knowledge/projects/general/index.md` lists
the live notes of no registered project. The pages are regenerated in the index
rebuild's transaction after a compile and at night, and in every registration
change's transaction.

The tests drive the compile command with the fake provider, the MCP tool as an
agent calls it, and the nightly step's own command, and read the pages a person
opens in Obsidian.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

DAY = "2026-09-20"
SLUG = "retry-needs-deadline"
LESSON = "A retry loop needs a deadline or it spins forever."
PROJECTS = Path("knowledge/projects")
GENERAL = PROJECTS / "general/index.md"


def _page(vault: Path, project: str) -> str:
    return (vault / PROJECTS / project / "index.md").read_text(encoding="utf-8")


def _sections(page: str) -> dict[str, list[str]]:
    """Each `##` heading of a page and the bullets under it, as a reader sees them."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in page.splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif line.startswith("# "):
            current = None
        elif current is not None and line.startswith("- "):
            current.append(line[2:])
    return sections


def _note(
    vault: Path,
    slug: str,
    *,
    kind: str,
    title: str,
    summary: str,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
) -> None:
    header = [f"type: {kind}", f'title: "{title}"']
    if project:
        header.append(f'project: "{project}"')
    if status:
        header.append(f"status: {status}")
    if tags:
        header.append(f"tags: [{', '.join(tags)}]")
    (vault / "knowledge/notes" / f"{slug}.md").write_text(
        "---\n" + "\n".join(header) + f"\n---\n\n# {title}\n\n"
        f"One-sentence summary: {summary}\n",
        encoding="utf-8",
    )


def _environment(vault: Path, **extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(vault.parent / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
        **extra,
    }


def _run(vault: Path, command: list[str], **extra: str) -> str:
    done = subprocess.run(
        command,
        env=_environment(vault, **extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _rebuild(vault: Path) -> str:
    return _run(vault, [sys.executable, str(SCRIPTS / "rebuild_memory_index.py")])


# --- the compile seam ------------------------------------------------------------------


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
    (vault / PROJECTS / "project-map.md").write_text(f"# Project map\n{sections}", encoding="utf-8")


def _daily(vault: Path, *, repository: str = "") -> None:
    location = f"- Project: `product-a`\n- Repository: `{repository}`\n" if repository else ""
    (vault / "knowledge/daily" / f"{DAY}.md").write_text(
        f"# Daily log {DAY}\n"
        "\n## [10:00:00] session-end | session-1000\n"
        "- Trigger: `session-end`\n"
        "- Agent: `claude`\n"
        f"{location}"
        "- Tier: `durable`\n\n"
        f"{LESSON}\n",
        encoding="utf-8",
    )


def _reply() -> str:
    operation = {
        "action": "create",
        "category": "patterns",
        "slug": SLUG,
        "title": "Retry needs a deadline",
        "summary": "A retry loop needs a deadline.",
        "body_section": "Lesson",
        "body_markdown": "Bound every retry loop by a deadline.",
        "evidence": [
            {
                "daily_date": DAY,
                "timestamp": "10:00:00",
                "quoted_text": LESSON,
                "claim": "A retry loop needs a deadline.",
            }
        ],
        "related": [],
    }
    draft = {"operations": [operation], "audit": {}}
    review = {"reviews": [{"slug": SLUG, "verdict": "pass", "reason": "ok"}]}
    return json.dumps(draft) + "\n" + json.dumps(review)


def _compile(vault: Path) -> None:
    _run(
        vault,
        [sys.executable, str(SCRIPTS / "compile_memory.py")],
        MEMORY_LLM_FAKE_RESPONSE=_reply(),
    )


RETRY_LINE = f"[[{SLUG}]] — A retry loop needs a deadline."


def test_a_compiled_note_appears_on_its_project_page_under_its_type(vault):
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend]})
    _daily(vault, repository=backend)
    _note(
        vault,
        "queue-is-at-least-once",
        kind="decision",
        title="The queue is at least once",
        summary="Delivery is at least once.",
        project="product-a",
        tags=["queue"],
    )
    _note(
        vault,
        "queue-is-exactly-once",
        kind="decision",
        title="The queue is exactly once",
        summary="Delivery is exactly once.",
        project="product-a",
        status="superseded",
    )

    _compile(vault)

    page = _page(vault, "product-a")
    sections = _sections(page)
    assert sections["Patterns"] == [RETRY_LINE]
    assert sections["Decisions"] == [
        "[[queue-is-at-least-once]] — Delivery is at least once."
    ]
    assert "queue-is-exactly-once" not in page
    assert sections["Modules"] == ["#queue (1 note)"]
    assert list(sections).index("Decisions") < list(sections).index("Patterns")
    assert page.startswith("---\ntype: project-context\n")
    assert "generated: true" in page.split("---")[1]
    assert SLUG not in (vault / GENERAL).read_text(encoding="utf-8")


def test_a_note_from_unregistered_work_appears_on_the_general_page(vault):
    _map(vault, {"product-a": [_checkout(vault, "backend")]})
    _daily(vault)

    _compile(vault)

    assert _sections((vault / GENERAL).read_text(encoding="utf-8"))["Patterns"] == [RETRY_LINE]
    assert SLUG not in _page(vault, "product-a")


def test_regenerating_writes_the_same_bytes_and_nothing_when_current(vault):
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend]})
    _daily(vault, repository=backend)
    _compile(vault)
    compiled = {
        path: path.read_bytes() for path in sorted((vault / PROJECTS).rglob("index.md"))
    }

    first = _rebuild(vault)
    second = _rebuild(vault)

    assert {path: path.read_bytes() for path in compiled} == compiled
    assert "rebuilt" not in first and "rebuilt" not in second
    assert "is current" in second


def test_the_pages_stay_private_and_out_of_the_tracked_index(vault):
    """The tracked index names published notes only; the project pages name them all."""
    shutil.copyfile(ROOT / ".gitignore", vault / ".gitignore")
    backend = _checkout(vault, "backend")
    _map(vault, {"product-a": [backend]})
    _daily(vault, repository=backend)

    _compile(vault)

    index = (vault / "knowledge/index.md").read_text(encoding="utf-8")
    assert SLUG not in index
    assert "projects/" not in index
    assert RETRY_LINE in _sections(_page(vault, "product-a"))["Patterns"]
    for page in ("knowledge/projects/product-a/index.md", GENERAL.as_posix()):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", page], cwd=ROOT, check=False
        )
        assert ignored.returncode == 0, page


# --- the nightly seam ------------------------------------------------------------------


def test_the_nightly_step_regenerates_the_pages_before_lint(vault):
    import scheduled_nightly

    steps = scheduled_nightly._post_compile_steps()
    labels = [step.label for step in steps]
    step = steps[labels.index("pages")]
    assert labels.index("pages") < labels.index("lint")
    _map(vault, {"product-a": []})
    _note(
        vault,
        "edited-by-hand",
        kind="concept",
        title="Edited by hand",
        summary="A note written in Obsidian.",
        project="product-a",
    )

    _run(vault, step.command)

    assert _sections(_page(vault, "product-a"))["Concepts"] == [
        "[[edited-by-hand]] — A note written in Obsidian."
    ]
    assert (vault / GENERAL).is_file()


# --- the MCP seam ----------------------------------------------------------------------


@pytest.fixture
def agent_vault(tmp_path, monkeypatch) -> Path:
    import memory_state

    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "projects").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir()
    (state / "run").mkdir(parents=True)
    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state))
    return root


def _repository(tmp_path: Path, name: str) -> Path:
    checkout = tmp_path / "work" / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src").mkdir()
    return checkout


def _call(arguments: dict) -> dict:
    import mcp_server

    return json.loads(asyncio.run(mcp_server._handle_tool_call("manage_project", arguments)))


def _pages(vault: Path) -> list[str]:
    root = vault / PROJECTS
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("index.md"))


def _work_state(vault: Path, project: str) -> list[str]:
    page = _page(vault, project)
    return page.split("## Work state", 1)[1].strip().splitlines()


def test_registration_changes_regenerate_move_and_remove_the_pages(agent_vault, tmp_path):
    vault = agent_vault
    backend = _repository(tmp_path, "backend")
    frontend = _repository(tmp_path, "frontend")
    _note(
        vault,
        "api-returns-problem-details",
        kind="pattern",
        title="The API returns problem details",
        summary="Errors are RFC 9457 problem details.",
        project="product-a",
    )
    _note(
        vault,
        "loose-lesson",
        kind="qa",
        title="A loose lesson",
        summary="Belongs to no project.",
    )

    created = _call({"action": "create", "name": "product-a", "directory": str(backend)})

    assert created["data"]["project_pages"]["written"] == [
        "knowledge/projects/general/index.md",
        "knowledge/projects/product-a/index.md",
    ]
    assert _pages(vault) == ["general/index.md", "product-a/index.md"]
    assert _sections(_page(vault, "product-a"))["Patterns"] == [
        "[[api-returns-problem-details]] — Errors are RFC 9457 problem details."
    ]
    assert _sections(_page(vault, "general"))["Q&A"] == [
        "[[loose-lesson]] — Belongs to no project."
    ]
    assert _work_state(vault, "product-a") == [
        "### backend",
        f"- Main checkout: `{backend.as_posix()}`",
        "- No work recorded yet.",
    ]

    _call({"action": "attach", "name": "product-a", "directory": str(frontend)})
    assert [line for line in _work_state(vault, "product-a") if line.startswith("###")] == [
        "### backend",
        "### frontend",
    ]

    renamed = _call({"action": "rename", "name": "product-a", "new_name": "product-b"})
    assert renamed["data"]["project_pages"]["deleted"] == ["knowledge/projects/product-a/index.md"]
    assert _pages(vault) == ["general/index.md", "product-b/index.md"]
    assert "### frontend" in _work_state(vault, "product-b")

    removed = _call({"action": "remove", "name": "product-b"})
    assert removed["data"]["project_pages"]["deleted"] == ["knowledge/projects/product-b/index.md"]
    assert _pages(vault) == ["general/index.md"]
    general = _sections(_page(vault, "general"))
    assert "[[api-returns-problem-details]] — Errors are RFC 9457 problem details." in general[
        "Patterns"
    ]


def test_a_project_page_shows_where_agents_left_off(agent_vault, tmp_path, monkeypatch):
    import integration_adapter

    vault = agent_vault
    monkeypatch.setattr(integration_adapter, "ROOT", vault)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", vault.parent / "state")
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    backend = _repository(tmp_path, "backend")
    (backend / ".git" / "HEAD").write_text("ref: refs/heads/feature-login\n", encoding="utf-8")
    _call({"action": "create", "name": "product-a", "directory": str(backend)})
    failed_command = {
        "session_id": "s-1",
        "cwd": str(backend),
        "tool_name": "Bash",
        "tool_input": {"command": "npm test"},
        "error": "Exit code 1",
    }
    with patch.object(sys, "stdin", io.StringIO(json.dumps(failed_command))):
        with patch.object(sys, "stdout", io.StringIO()):
            arguments = ["--source", "claude", "--event", "post_tool_use"]
            arguments += ["--checkpoint-type", "significant_failure"]
            assert integration_adapter.main(arguments) == 0

    _call({"action": "create", "name": "product-b"})

    lines = _work_state(vault, "product-a")
    assert "- Work state: [[projects/product-a/backend/state]]" in lines
    assert "- Branch: `feature-login`" in lines
    blockers = [line for line in lines if line.startswith("- Open blockers: 1, newest:")]
    assert len(blockers) == 1 and "npm test" in blockers[0]
    assert _work_state(vault, "product-b") == ["No repository is attached yet."]


def test_a_general_project_cannot_be_registered(agent_vault):
    refused = _call({"action": "create", "name": "General"})

    assert refused["data"]["code"] == "reserved_name"
    assert _pages(agent_vault) == []
