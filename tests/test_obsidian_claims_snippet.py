"""The installer places the claims-ledger snippet in an existing Obsidian vault (issue #11).

ADR 0003: Obsidian is the owner's reading surface, so the product may ship viewer
files, but nothing may make Obsidian required. The snippet is copied only when
`knowledge/.obsidian/` already exists, and the install transaction owns it so an
uninstall takes it back.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import install_control  # noqa: E402

SNIPPET = Path("integrations") / "obsidian" / "llm-wiki-claims-ledger.css"
RELEASE = {
    "commit_oid": "a" * 40,
    "project_version": "0.0.0",
    "source_mode": "pinned_remote",
    "uv_lock_sha256": "b" * 64,
    "worktree_clean": True,
}


@pytest.fixture
def vault(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "vault"
    (root / "knowledge").mkdir(parents=True)
    (root / SNIPPET).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / SNIPPET, root / SNIPPET)
    target = tmp_path / "cron.txt"

    def write(value: bytes | None) -> None:
        if value is None:
            target.unlink(missing_ok=True)
            return
        target.write_bytes(value)

    def scheduler(**_arguments: object) -> install_control.ManagedResource:
        return install_control.ManagedResource(
            resource_id="cron-scheduler",
            kind="test_scheduler",
            locator=str(target),
            desired=b"cron",
            read_owned=lambda: target.read_bytes() if target.exists() else None,
            write_owned=write,
            recognizes=lambda current: current == b"cron",
        )

    monkeypatch.setattr(install_control, "_selected_backend", lambda _requested: "cron")
    monkeypatch.setattr(install_control, "_posix_scheduler_resource", scheduler)
    monkeypatch.setattr(install_control, "build_release_identity", lambda _root: RELEASE)
    return root


def _args(vault: Path) -> argparse.Namespace:
    home = vault.parent / "home"
    home.mkdir(exist_ok=True)
    return argparse.Namespace(
        root=vault, state_root=vault.parent / "state", uv_path=vault.parent / "uv", home=home,
        scheduler="cron", profile=home / ".profile", powershell_path=None,
        opencode_plugin=False, claude_settings=False, codex_hooks=False,
    )


def test_an_obsidian_vault_gets_the_snippet_and_an_uninstall_takes_it_back(vault: Path) -> None:
    (vault / "knowledge" / ".obsidian").mkdir()
    installed = vault / "knowledge" / ".obsidian" / "snippets" / "llm-wiki-claims-ledger.css"

    install_control._install_from_args(_args(vault))

    assert installed.read_bytes() == (ROOT / SNIPPET).read_bytes()

    install_control._uninstall_from_args(_args(vault))

    assert not installed.exists()
    assert (vault / "knowledge" / ".obsidian").is_dir()


def test_a_vault_without_obsidian_gets_no_obsidian_folder(vault: Path) -> None:
    install_control._install_from_args(_args(vault))

    assert not (vault / "knowledge" / ".obsidian").exists()


def test_a_rerun_leaves_the_installed_snippet_as_it_is(vault: Path) -> None:
    (vault / "knowledge" / ".obsidian").mkdir()
    installed = vault / "knowledge" / ".obsidian" / "snippets" / "llm-wiki-claims-ledger.css"
    install_control._install_from_args(_args(vault))

    result = install_control._install_from_args(_args(vault))

    assert result["replaced"] is False
    assert installed.read_bytes() == (ROOT / SNIPPET).read_bytes()


def test_obsidian_opened_after_install_gets_the_snippet_on_the_next_run(vault: Path) -> None:
    install_control._install_from_args(_args(vault))
    (vault / "knowledge" / ".obsidian").mkdir()

    install_control._install_from_args(_args(vault))

    assert (vault / "knowledge" / ".obsidian" / "snippets" / "llm-wiki-claims-ledger.css").is_file()


def test_the_snippet_targets_the_ledger_in_reading_and_live_preview() -> None:
    css = (ROOT / SNIPPET).read_text(encoding="utf-8")

    assert '.el-h2:has(h2[data-heading="Claims"]) + .el-pre' in css
    assert ".HyperMD-header-2 + .HyperMD-codeblock-begin + .HyperMD-codeblock" in css


def test_the_user_guide_says_how_to_enable_the_snippet() -> None:
    guide = (ROOT / "docs" / "USER-GUIDE.md").read_text(encoding="utf-8")

    assert "llm-wiki-claims-ledger" in guide
    assert "CSS snippets" in guide
