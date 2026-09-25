"""The owner registers projects by asking an agent, and can edit the map by hand.

Stage 2 of `docs/specs/2026-09-24-readable-memory.md`, ADR 0002: a project is a
product made of repositories, listed in one private Markdown file under
`knowledge/projects/`. The agent edits it through the `manage_project` MCP tool;
the owner edits it in Obsidian; doctor names the entries nothing can use.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

MAP = Path("knowledge/projects/project-map.md")


@pytest.fixture
def vault(tmp_path, monkeypatch):
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


def _repository(parent: Path, name: str) -> Path:
    checkout = parent / "work" / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src").mkdir()
    return checkout


def _call(arguments: dict) -> dict:
    import mcp_server

    return json.loads(
        asyncio.run(mcp_server._handle_tool_call("manage_project", arguments))
    )


def _map_text(vault: Path) -> str:
    return (vault / MAP).read_text(encoding="utf-8")


def _sections(vault: Path) -> dict[str, list[str]]:
    """What a reader of the file sees: each `##` heading and the bullets under it."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in _map_text(vault).splitlines():
        if line.startswith("## "):
            current = sections.setdefault(line[3:].strip(), [])
        elif line.startswith("# "):
            current = None
        elif current is not None and line.startswith("- "):
            current.append(line[2:].strip())
    return sections


def _posix(path: Path) -> str:
    return path.as_posix()


def test_the_map_follows_create_attach_rename_detach_and_remove(vault, tmp_path):
    backend = _repository(tmp_path, "backend")
    frontend = _repository(tmp_path, "frontend")

    created = _call({"action": "create", "name": "Product A", "directory": str(backend / "src")})
    assert created["data"]["status"] == "ok"
    assert created["data"]["project"] == "product-a"
    assert _sections(vault) == {"product-a": [_posix(backend)]}
    assert _map_text(vault).startswith("---\ntype: project-context\n---\n")

    attached = _call({"action": "attach", "name": "product-a", "directory": str(frontend)})
    assert attached["data"]["changed"] is True
    assert _sections(vault) == {"product-a": [_posix(backend), _posix(frontend)]}

    renamed = _call({"action": "rename", "name": "product-a", "new_name": "Product B"})
    assert renamed["data"]["project"] == "product-b"
    assert _sections(vault) == {"product-b": [_posix(backend), _posix(frontend)]}

    detached = _call({"action": "detach", "directory": str(backend)})
    assert detached["data"]["detached_from"] == ["product-b"]
    assert _sections(vault) == {"product-b": [_posix(frontend)]}

    listed = _call({"action": "list"})
    assert listed["data"]["projects"] == [
        {"name": "product-b", "repositories": [_posix(frontend)]}
    ]

    removed = _call({"action": "remove", "name": "product-b"})
    assert removed["data"]["detached"] == [_posix(frontend)]
    assert _sections(vault) == {}
    assert "error" not in removed["data"]
    assert removed["partial"] is False


def test_attaching_a_repository_of_another_project_moves_it_and_says_so(vault, tmp_path):
    shared = _repository(tmp_path, "shared")
    _call({"action": "create", "name": "alpha", "directory": str(shared)})
    _call({"action": "create", "name": "beta"})

    moved = _call({"action": "attach", "name": "beta", "directory": str(shared / "src")})

    assert moved["data"]["moved_from"] == ["alpha"]
    assert "moved from 'alpha'" in moved["data"]["message"]
    assert _sections(vault) == {"alpha": [], "beta": [_posix(shared)]}


def test_an_adopted_vault_writes_the_map_through_its_coordinator(tmp_path, monkeypatch):
    """The owner's vault runs the adopted coordinator, not the pre-adoption one."""
    import memory_state

    from tests.adopted_capture_vault import adopted_capture_vault

    state_root, _project = adopted_capture_vault(tmp_path, monkeypatch, memory_state)
    backend = _repository(tmp_path, "backend")

    created = _call({"action": "create", "name": "alpha", "directory": str(backend)})

    assert created["data"]["status"] == "ok", created
    assert _sections(tmp_path / "vault") == {"alpha": [_posix(backend)]}
    assert (state_root / "run" / "markdown-transactions-v3.sqlite3").is_file()


def test_a_worktree_registers_its_main_checkout(vault, tmp_path):
    checkout = _repository(tmp_path, "backend")
    admin = checkout / ".git" / "worktrees" / "feature"
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "elsewhere" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")

    _call({"action": "create", "name": "alpha", "directory": str(worktree)})

    assert _sections(vault) == {"alpha": [_posix(checkout)]}


@pytest.mark.parametrize(
    ("setup", "arguments", "code"),
    [
        (None, {"action": "attach", "name": "ghost", "directory": "{repo}"}, "unknown_project"),
        (None, {"action": "create", "name": "general"}, "reserved_name"),
        (None, {"action": "create", "name": "!!"}, "invalid_name"),
        ("alpha", {"action": "create", "name": "Alpha"}, "project_exists"),
        ("alpha", {"action": "attach", "name": "alpha", "directory": "{plain}"}, "not_a_repository"),
        ("alpha", {"action": "detach", "directory": "{repo}"}, "not_attached"),
        ("alpha", {"action": "rename", "name": "alpha", "new_name": "general"}, "reserved_name"),
        (None, {"action": "remove", "name": "ghost"}, "unknown_project"),
    ],
)
def test_a_refused_request_names_its_reason_and_changes_nothing(
    vault, tmp_path, setup, arguments, code
):
    repo = _repository(tmp_path, "backend")
    plain = tmp_path / "downloads"
    plain.mkdir()
    if setup:
        _call({"action": "create", "name": setup})
    before = (vault / MAP).read_bytes() if (vault / MAP).exists() else None
    filled = {
        key: value.format(repo=repo, plain=plain) if isinstance(value, str) else value
        for key, value in arguments.items()
    }

    envelope = _call(filled)

    assert envelope["data"]["code"] == code
    assert envelope["data"]["error"]
    assert envelope["partial"] is True
    after = (vault / MAP).read_bytes() if (vault / MAP).exists() else None
    assert after == before


def test_an_attach_without_a_directory_is_refused_before_dispatch(vault):
    envelope = _call({"action": "attach", "name": "alpha"})

    assert "directory" in envelope["data"]["error"]
    assert not (vault / MAP).exists()


def test_the_parser_tolerates_a_hand_edited_map_and_edits_keep_the_prose(vault, tmp_path):
    backend = _repository(tmp_path, "backend")
    frontend = _repository(tmp_path, "frontend")
    written_backend = str(backend).replace("/", "\\") + "\\"
    written_frontend = frontend.as_posix() + "/"
    if os.name == "nt":
        written_frontend = written_frontend.upper()
    (vault / MAP).write_text(
        "---\ntype: project-context\n---\n# My projects\n\n"
        "Some notes I keep about how I organise work.\n\n"
        "##   Product A  \n\n"
        "The customer-facing product.\n\n"
        f"* `{written_backend}`\n\n\n"
        f"-   {written_frontend}\n"
        "### Ideas\n\n"
        "Maybe split the API later.\n",
        encoding="utf-8",
    )

    listed = _call({"action": "list"})["data"]
    assert [project["name"] for project in listed["projects"]] == ["product-a"]
    assert len(listed["projects"][0]["repositories"]) == 2
    assert listed["problems"] == []

    again = _call({"action": "attach", "name": "product-a", "directory": str(frontend)})
    assert again["data"]["changed"] is False

    detached = _call({"action": "detach", "directory": str(backend / "src")})
    assert detached["data"]["detached_from"] == ["product-a"]
    text = _map_text(vault)
    assert "Some notes I keep about how I organise work." in text
    assert "The customer-facing product." in text
    assert "Maybe split the API later." in text
    assert written_backend not in text
    assert written_frontend in text


def test_a_listed_path_that_no_longer_exists_can_still_be_detached(vault, tmp_path):
    gone = tmp_path / "work" / "retired"
    (vault / MAP).write_text(f"## alpha\n\n- {gone.as_posix()}\n", encoding="utf-8")

    detached = _call({"action": "detach", "directory": str(gone)})

    assert detached["data"]["detached_from"] == ["alpha"]
    assert _sections(vault) == {"alpha": []}


def _projects_check(vault: Path, tmp_path: Path) -> dict:
    import doctor

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    report = doctor.run_doctor(root=vault, state_root=tmp_path / "state", home=home)
    return next(check for check in report["checks"] if check["id"] == "projects")


def test_doctor_reports_every_entry_the_map_cannot_use(vault, tmp_path):
    backend = _repository(tmp_path, "backend")
    (vault / MAP).write_text(
        "# Project map\n\n"
        f"## alpha\n- {backend.as_posix()}\n- {(tmp_path / 'missing').as_posix()}\n"
        f"- {(backend / 'src').as_posix()}\n\n"
        f"## Alpha\n\n## beta\n- {backend.as_posix()}\n\n"
        "## general\n\n## !!\n",
        encoding="utf-8",
    )

    check = _projects_check(vault, tmp_path)

    assert check["status"] == "degraded"
    codes = sorted(problem["code"] for problem in check["details"]["problems"])
    assert codes == [
        "duplicate_project",
        "invalid_name",
        "missing_repository",
        "not_repository_root",
        "repository_in_two_projects",
        "reserved_name",
    ]
    assert re.search(r"6 invalid entries", check["message"])


def test_doctor_is_quiet_about_a_valid_map_and_an_absent_one(vault, tmp_path):
    assert _projects_check(vault, tmp_path)["status"] == "ok"
    backend = _repository(tmp_path, "backend")
    _call({"action": "create", "name": "alpha", "directory": str(backend)})

    check = _projects_check(vault, tmp_path)

    assert (check["status"], check["details"]["projects"]) == ("ok", 1)


def test_the_map_is_private():
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", MAP.as_posix()],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
