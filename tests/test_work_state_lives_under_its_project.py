"""Work state lives under the project a repository is registered in; other work creates nothing.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002. A registered
repository's journal and generated state live at
`knowledge/projects/<project>/<repository>/`, whichever checkout, subfolder or
worktree the agent works in. A directory whose repository the project map does not
list gets no journal, no state and no folder, while its daily-log capture goes on.
Registering, moving, renaming, detaching and removing through the MCP tool keeps the
folders in step with the map, in one undoable transaction.

Every test drives the product as a host hook or an agent does and reads what a
person or the next agent reads: the files, the daily log, the hook's context and the
tool's response envelope.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402

MAP = Path("knowledge/projects/project-map.md")


@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    import memory_state

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "knowledge" / "projects").mkdir(parents=True)
    (root / "knowledge" / "notes").mkdir()
    (state_root / "run").mkdir(parents=True)
    monkeypatch.setattr(integration_adapter, "ROOT", root)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    return root


def _repository(tmp_path: Path, name: str = "backend") -> Path:
    checkout = tmp_path / f"work-{uuid.uuid4().hex[:8]}" / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (checkout / "src").mkdir()
    return checkout


def _worktree(checkout: Path, location: Path) -> Path:
    """What `git worktree add` leaves: a pointer file, and the admin dir it names."""
    admin = checkout / ".git" / "worktrees" / location.name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    location.mkdir(parents=True)
    (location / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return location


def _register(vault: Path, projects: dict[str, list[Path]]) -> None:
    """The map as the owner writes it by hand in Obsidian."""
    lines = ["# Project map", ""]
    for name, repositories in projects.items():
        lines += [f"## {name}", "", *(f"- {path.as_posix()}" for path in repositories), ""]
    (vault / MAP).write_text("\n".join(lines), encoding="utf-8")


def _session() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


def _hook(argv: list[str], raw: dict) -> str:
    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        with patch.object(sys, "stdout", io.StringIO()) as out:
            assert integration_adapter.main(argv) == 0
            return out.getvalue()


def _edit_in(directory: Path, session: str | None = None, *, failed: bool = False) -> None:
    raw = {
        "session_id": session or _session(),
        "cwd": str(directory),
        "tool_name": "Edit" if not failed else "Bash",
        "tool_input": {"file_path": "src/app.py"} if not failed else {"command": "npm test"},
    }
    extra = ["--checkpoint-type", "significant_failure"] if failed else []
    if failed:
        raw["error"] = "Exit code 1"
    _hook(["--source", "claude", "--event", "post_tool_use", *extra], raw)


def _session_start(directory: Path) -> str:
    return _hook(
        ["--source", "claude", "--event", "session_start"],
        {"session_id": _session(), "cwd": str(directory), "source": "startup"},
    )


def _session_end(directory: Path, session: str) -> None:
    """A session end without a transcript, tagged the way the Codex wrapper asks."""
    envelope = integration_adapter.normalize_event(
        "claude", "session_end", {"session_id": session, "cwd": str(directory), "reason": "exit"}
    )
    integration_adapter.ingest_event(envelope, force_stub=True)


def _projects_area(vault: Path) -> list[str]:
    root = vault / "knowledge" / "projects"
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def _project_files(vault: Path) -> list[str]:
    root = vault / "knowledge" / "projects"
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _journals(vault: Path) -> list[str]:
    root = vault / "knowledge" / "projects"
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("journal.md"))


def _events(journal: Path) -> list[dict]:
    lines = journal.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


def _frontmatter(page: Path) -> str:
    return page.read_text(encoding="utf-8").split("---", 2)[1]


def _daily(vault: Path) -> str:
    return "".join(
        page.read_text(encoding="utf-8")
        for page in sorted((vault / "knowledge" / "daily").glob("*.md"))
    )


# --- lifecycle events -------------------------------------------------------------


def test_the_checkout_a_subfolder_and_a_worktree_write_one_journal_under_the_project(
    vault, tmp_path
) -> None:
    checkout = _repository(tmp_path)
    worktree = _worktree(checkout, tmp_path / "worktrees" / "feature-x")
    _register(vault, {"product-a": [checkout]})

    _edit_in(checkout)
    _edit_in(checkout / "src")
    _edit_in(worktree)

    assert _journals(vault) == ["product-a/backend/journal.md"]
    folder = vault / "knowledge" / "projects" / "product-a" / "backend"
    events = _events(folder / "journal.md")
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert {event["provenance"]["worktree"] for event in events} == {str(checkout.resolve())}
    frontmatter = _frontmatter(folder / "state.md")
    assert 'project: "product-a"' in frontmatter
    assert 'repository: "backend"' in frontmatter
    assert "# product-a/backend - State" in (folder / "state.md").read_text(encoding="utf-8")


def test_two_repositories_of_one_project_get_a_folder_each(vault, tmp_path) -> None:
    backend = _repository(tmp_path, "backend")
    frontend = _repository(tmp_path, "frontend")
    _register(vault, {"product-a": [backend, frontend]})

    _edit_in(backend)
    _edit_in(frontend)

    assert _journals(vault) == [
        "product-a/backend/journal.md",
        "product-a/frontend/journal.md",
    ]


def test_same_named_repositories_are_told_apart_within_their_project(vault, tmp_path) -> None:
    first = _repository(tmp_path, "api")
    second = _repository(tmp_path, "api")
    _register(vault, {"product-a": [first, second]})

    _edit_in(first)
    _edit_in(second)

    journals = _journals(vault)
    assert len(journals) == 2
    assert "product-a/api/journal.md" in journals
    assert all(journal.startswith("product-a/api") for journal in journals)


def test_unregistered_work_creates_nothing_in_the_projects_area(vault, tmp_path) -> None:
    registered = _repository(tmp_path, "backend")
    unregistered = _repository(tmp_path, "scratch")
    plain = tmp_path / "downloads"
    plain.mkdir()
    _register(vault, {"product-a": [registered]})
    before = _projects_area(vault)
    session = _session()

    for directory in (unregistered, unregistered / "src", plain):
        _session_start(directory)
        _hook(
            ["--source", "claude", "--event", "user_prompt"],
            {"session_id": session, "cwd": str(directory), "prompt": "look up the release notes"},
        )
        _edit_in(directory, session)
        _edit_in(directory, session, failed=True)
        _session_end(directory, session)

    assert _projects_area(vault) == before == ["project-map.md"]
    daily = _daily(vault)
    assert "look up the release notes" in daily
    assert "| scratch" not in daily and "scratch`" not in daily
    assert "- Project:" not in daily and "Project slug" not in daily


def test_a_registered_repository_names_its_project_and_repository_in_the_daily_log(
    vault, tmp_path
) -> None:
    checkout = _repository(tmp_path)
    _register(vault, {"product-a": [checkout]})
    session = _session()

    _hook(
        ["--source", "claude", "--event", "user_prompt"],
        {"session_id": session, "cwd": str(checkout / "src"), "prompt": "fix the login redirect"},
    )
    _session_end(checkout / "src", session)

    daily = _daily(vault)
    assert f"| {session[:8]} | product-a/backend` fix the login redirect" in daily
    assert "- Project: `product-a`" in daily
    assert f"- Repository: `{checkout.resolve()}`" in daily


def test_session_start_hands_off_the_work_state_it_finds_under_the_project(
    vault, tmp_path
) -> None:
    checkout = _repository(tmp_path)
    _register(vault, {"product-a": [checkout]})
    _edit_in(checkout, failed=True)

    context = _session_start(checkout / "src")

    assert "Project handoff: product-a/backend" in context
    assert "Bash failed: npm test" in context


def test_the_user_level_session_start_hook_reads_the_new_layout_and_creates_nothing_else(
    vault, tmp_path
) -> None:
    checkout = _repository(tmp_path)
    unregistered = _repository(tmp_path, "scratch")
    _register(vault, {"product-a": [checkout]})
    _edit_in(checkout, failed=True)
    before = _projects_area(vault)

    def hook(directory: Path) -> str:
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(directory)}
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "session_start_project_state.py")],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=True,
            timeout=60,
        )
        return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "Bash failed: npm test" in hook(checkout / "src")
    assert hook(unregistered) == ""
    assert _projects_area(vault) == before


def test_a_folder_from_before_the_project_layout_is_left_alone(vault, tmp_path) -> None:
    """Issue #18 migrates these; until then nothing reads them as work state or trips on them."""
    legacy = vault / "knowledge" / "projects" / "backend"
    legacy.mkdir()
    (legacy / "journal.md").write_text("not a journal\n", encoding="utf-8")
    (legacy / "state.md").write_text("# backend - State\n- Project root: `/old`\n", encoding="utf-8")
    checkout = _repository(tmp_path)
    _register(vault, {"backend": [checkout]})

    _edit_in(checkout)
    context = _session_start(checkout)

    assert (legacy / "journal.md").read_text(encoding="utf-8") == "not a journal\n"
    assert _journals(vault) == ["backend/backend/journal.md", "backend/journal.md"]
    assert "Project handoff: backend/backend" in context


# --- the registration tool keeps the folders in step --------------------------------


def _call(arguments: dict) -> dict:
    import mcp_server

    return json.loads(asyncio.run(mcp_server._handle_tool_call("manage_project", arguments)))


def _doctor(arguments: dict) -> dict:
    import mcp_server

    return json.loads(asyncio.run(mcp_server._handle_tool_call("doctor", arguments)))


def _folder(vault: Path, relative: str) -> Path:
    return vault / "knowledge" / "projects" / relative


@pytest.fixture
def worked(vault, tmp_path):
    """A registered repository with work state, and a note of its project."""
    checkout = _repository(tmp_path)
    created = _call({"action": "create", "name": "product-a", "directory": str(checkout)})
    assert created["data"]["status"] == "ok"
    _edit_in(checkout)
    note = vault / "knowledge" / "notes" / "a-lesson.md"
    note.write_text("---\ntype: pattern\nproject: product-a\n---\n# A lesson\n", encoding="utf-8")
    return checkout, note


def test_attaching_the_repository_to_another_project_moves_its_work_state(
    vault, worked
) -> None:
    checkout, note = worked
    _call({"action": "create", "name": "product-b"})

    response = _call({"action": "attach", "name": "product-b", "directory": str(checkout)})

    assert response["data"]["status"] == "ok"
    assert response["data"]["work_state"]["moved"] == [
        {
            "from": "knowledge/projects/product-a/backend",
            "to": "knowledge/projects/product-b/backend",
        }
    ]
    assert "work state moved to knowledge/projects/product-b/backend" in response["data"]["message"]
    assert _project_files(vault) == [
        "general/index.md",
        "product-a/index.md",
        "product-b/backend/journal.md",
        "product-b/backend/state.md",
        "product-b/index.md",
        "project-map.md",
    ]
    assert 'project: "product-b"' in _frontmatter(_folder(vault, "product-b/backend/state.md"))

    _edit_in(checkout)

    events = _events(_folder(vault, "product-b/backend/journal.md"))
    assert [event["sequence"] for event in events] == [1, 2]
    assert note.read_text(encoding="utf-8").endswith("# A lesson\n")


def test_renaming_the_project_moves_its_folder_and_the_journal_goes_on(vault, worked) -> None:
    checkout, note = worked
    before = note.read_bytes()

    response = _call({"action": "rename", "name": "product-a", "new_name": "product-c"})

    assert response["data"]["work_state"]["moved"] == [
        {"from": "knowledge/projects/product-a", "to": "knowledge/projects/product-c"}
    ]
    assert not [name for name in _project_files(vault) if name.startswith("product-a/")]
    assert "# product-c/backend - State" in _folder(
        vault, "product-c/backend/state.md"
    ).read_text(encoding="utf-8")

    _edit_in(checkout / "src")

    assert _journals(vault) == ["product-c/backend/journal.md"]
    events = _events(_folder(vault, "product-c/backend/journal.md"))
    assert [event["sequence"] for event in events] == [1, 2]
    assert note.read_bytes() == before.replace(b"project: product-a", b'project: "product-c"')
    assert response["data"]["notes"]["renamed"] == ["knowledge/notes/a-lesson.md"]


def test_detaching_deletes_the_work_state_and_the_undo_brings_it_back(vault, worked) -> None:
    checkout, note = worked
    journal = _folder(vault, "product-a/backend/journal.md")
    kept = journal.read_bytes()

    response = _call({"action": "detach", "directory": str(checkout)})

    work_state = response["data"]["work_state"]
    assert work_state["deleted"] == ["knowledge/projects/product-a/backend"]
    assert "two days" in work_state["undo"]
    assert "undoable" in response["data"]["message"]
    assert _journals(vault) == []
    assert note.exists()

    _edit_in(checkout)
    assert _journals(vault) == []

    undone = _doctor(
        {"action": "transaction-undo", "target_id": work_state["transaction"], "repair": True}
    )

    assert undone["data"]["overall_status"] == "ok", undone
    assert journal.read_bytes() == kept


def test_a_repository_attached_again_after_a_detach_starts_a_fresh_journal(
    vault, worked
) -> None:
    checkout, _note = worked
    _call({"action": "detach", "directory": str(checkout)})

    _call({"action": "attach", "name": "product-a", "directory": str(checkout)})
    _edit_in(checkout)

    events = _events(_folder(vault, "product-a/backend/journal.md"))
    assert [event["sequence"] for event in events] == [1]


def test_removing_the_project_deletes_its_folder_and_leaves_the_notes(vault, worked) -> None:
    checkout, note = worked
    before = note.read_bytes()

    response = _call({"action": "remove", "name": "product-a"})

    assert response["data"]["work_state"]["deleted"] == ["knowledge/projects/product-a"]
    assert _project_files(vault) == ["general/index.md", "project-map.md"]
    assert note.read_bytes() == before

    _edit_in(checkout)
    assert _project_files(vault) == ["general/index.md", "project-map.md"]


def test_an_emptied_folder_is_removed_once_its_undo_window_has_passed(vault, worked) -> None:
    """The undo needs the directories it put the files back into; after two days nothing does."""
    checkout, _note = worked
    _call({"action": "detach", "directory": str(checkout)})
    emptied = _folder(vault, "product-a/backend")
    assert emptied.is_dir()
    three_days_ago = time.time() - 3 * 86400
    os.utime(emptied, (three_days_ago, three_days_ago))

    _call({"action": "list"})
    _call({"action": "create", "name": "product-b"})

    assert not emptied.exists()
    assert _projects_area(vault) == [
        "general",
        "general/index.md",
        "product-a",
        "product-a/index.md",
        "product-b",
        "product-b/index.md",
        "project-map.md",
    ]


# --- readers ------------------------------------------------------------------------


def test_the_context_tool_finds_the_work_state_under_the_project(vault, tmp_path) -> None:
    import mcp_server

    checkout = _repository(tmp_path)
    _register(vault, {"product-a": [checkout]})
    _edit_in(checkout, failed=True)

    package = mcp_server._get_context(["state"], token_budget=1200)

    active = package["active_task"]
    assert {item["source"] for item in active} == {"knowledge/projects/product-a/backend/state.md"}
    assert {item["project"] for item in active} == {"product-a"}
    assert any("Bash failed: npm test" in item["text"] for item in active)
    assert "knowledge/projects/project-map.md" not in json.dumps(package)


def test_session_start_counts_the_registered_projects_not_the_folders(vault, tmp_path) -> None:
    import session_start_context

    stray = vault / "knowledge" / "projects" / "scratch"
    stray.mkdir()
    (stray / "state.md").write_text("# scratch - State\n", encoding="utf-8")
    _register(vault, {"product-a": [_repository(tmp_path)], "product-b": []})

    with patch.object(session_start_context, "ROOT", vault):
        block = session_start_context.metacognitive_block()

    assert "2 active project(s)" in block
