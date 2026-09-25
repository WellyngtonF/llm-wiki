"""A repository is its main checkout: its subfolders and worktrees are the same repository.

An agent's working directory follows its `cd`, and a T3 Code worktree lives outside the
repository it belongs to. Both used to mint a project of their own, named after the
subfolder or the worktree. Stage 2 of `docs/specs/2026-09-24-readable-memory.md`,
ADR 0002. Since issue #14 only a registered repository has work state, so each test
registers the directory it expects to resolve to, and an unregistered one gets none.
"""
from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from tests.adopted_capture_vault import adopted_capture_vault  # noqa: E402


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    """(integration_adapter, vault, home) on an adopted vault, with a home of its own."""
    import capture_diagnostics
    import integration_adapter
    import memory_state

    adopted_capture_vault(tmp_path, monkeypatch, integration_adapter)
    run = tmp_path / "adapter-state"
    monkeypatch.setattr(memory_state, "STATE_DIR", run)
    monkeypatch.setattr(memory_state, "STATE_FILE", run / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", run / "state.json.lock")
    monkeypatch.setattr(capture_diagnostics, "FAILURE_LOG", run / "capture-failures.jsonl")
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda _args: 1)
    monkeypatch.setattr(
        integration_adapter, "_run_delegate", lambda *_a, **_k: Namespace(returncode=0)
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return integration_adapter, tmp_path / "vault", home


def _register(vault: Path, project: str, *directories: Path) -> None:
    """The project map as the owner writes it by hand."""
    bullets = "".join(f"- {directory.resolve().as_posix()}\n" for directory in directories)
    (vault / "knowledge" / "projects" / "project-map.md").write_text(
        f"## {project}\n\n{bullets}", encoding="utf-8"
    )


def _repository(parent: Path, name: str) -> Path:
    checkout = parent / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    return checkout


def _worktree(checkout: Path, location: Path) -> Path:
    """What `git worktree add` leaves: a pointer file, and the admin dir it names."""
    admin = checkout / ".git" / "worktrees" / location.name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{location / '.git'}\n", encoding="utf-8")
    location.mkdir(parents=True)
    (location / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return location


def _edit_from(adapter_module, directory: Path, event_id: str) -> dict:
    envelope = adapter_module.normalize_event(
        "claude",
        "post_tool_use",
        {
            "session_id": "s1",
            "cwd": str(directory),
            "event_id": event_id,
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/app.py"},
            "changed": True,
            "significant": True,
        },
    )
    return adapter_module.ingest_event(envelope)


def _projects(vault: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in (vault / "knowledge" / "projects").iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    )


def _journal_roots(vault: Path, folder: str) -> list[str]:
    text = (vault / "knowledge" / "projects" / folder / "journal.md").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [event["provenance"]["worktree"] for event in events]


def test_a_subfolder_and_a_worktree_carry_the_repository_of_their_main_checkout(adapter):
    module, vault, home = adapter
    checkout = _repository(home / "code", "alpha")
    subfolder = checkout / "src" / "backend"
    subfolder.mkdir(parents=True)
    worktree = _worktree(checkout, home / ".t3" / "worktrees" / "alpha" / "feature-x")
    (worktree / "src").mkdir()
    _register(vault, "product-a", checkout)

    results = [
        _edit_from(module, subfolder, "from-subfolder"),
        _edit_from(module, worktree / "src", "from-worktree"),
        _edit_from(module, checkout, "from-checkout"),
    ]

    assert [result["slug"] for result in results] == ["product-a/alpha"] * 3
    assert _projects(vault) == ["product-a"]
    assert set(_journal_roots(vault, "product-a/alpha")) == {str(checkout.resolve())}
    state = (vault / "knowledge" / "projects" / "product-a" / "alpha" / "state.md").read_text(
        encoding="utf-8"
    )
    assert f"- Project root: `{checkout.resolve()}`" in state


def test_resolution_never_climbs_to_the_home_directory(adapter):
    """A dotfiles repository at home does not swallow every directory under it."""
    module, vault, home = adapter
    (home / ".git").mkdir()
    scratch = home / "scratch" / "notes"
    scratch.mkdir(parents=True)
    _register(vault, "product-a", scratch)

    result = _edit_from(module, scratch, "from-scratch")

    assert result["slug"] == "product-a/notes"
    assert _journal_roots(vault, "product-a/notes") == [str(scratch.resolve())]


def test_resolution_never_climbs_to_a_directory_that_holds_the_vault(adapter):
    module, vault, _home = adapter
    (vault.parent / ".git").mkdir()
    sibling = vault.parent / "sibling"
    sibling.mkdir()
    _register(vault, "product-a", sibling)

    result = _edit_from(module, sibling, "from-sibling")

    assert result["slug"] == "product-a/sibling"
    assert _projects(vault) == ["product-a"]


def test_an_unregistered_repository_has_no_work_state(adapter):
    module, vault, home = adapter
    checkout = _repository(home / "code", "beta")
    _register(vault, "product-a", _repository(home / "code", "alpha"))

    result = _edit_from(module, checkout, "from-unregistered")

    assert result["slug"] is None
    assert _projects(vault) == []


def test_a_worktree_of_the_vault_is_the_vault(adapter):
    """The vault is never a project, whichever checkout of it the agent works in."""
    module, vault, home = adapter
    (vault / ".git").mkdir()
    worktree = _worktree(vault, home / "worktrees" / "vault-feature")

    result = _edit_from(module, worktree, "from-vault-worktree")

    assert result["slug"] is None
    assert _projects(vault) == []
