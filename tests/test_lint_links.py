"""Lint follows a link the way Obsidian does, with the vault rooted at `knowledge/`.

A bare `[[slug]]` names a file anywhere in the vault; a link with a slash is a
path from the vault root. Backlinks are Obsidian's to derive, so a one-way link
is not a finding (ADR 0003).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

_NOTE = "---\ntype: concept\n---\n# {title}\n\n{body}\n"
_LINKS = (
    "[[beta]]",
    "[[beta|the second note]]",
    "[[beta#Heading]]",
    "[[notes/deeper/gamma]]",
    "[[gamma]]",
    "[[projects/product-a/state]]",
    "[[inbox/clip.pdf]]",
    "[[knowledge/notes/beta]]",
    "[[missing-note]]",
)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    (notes / "deeper").mkdir(parents=True)
    (vault / "knowledge" / "projects" / "product-a").mkdir(parents=True)
    (vault / "knowledge" / "inbox").mkdir(parents=True)
    (notes / "alpha.md").write_text(
        _NOTE.format(title="Alpha", body="\n".join(f"- {link}" for link in _LINKS)),
        encoding="utf-8",
    )
    (notes / "beta.md").write_text(_NOTE.format(title="Beta", body="## Heading"), encoding="utf-8")
    (notes / "deeper" / "gamma.md").write_text(
        _NOTE.format(title="Gamma", body="Gamma."), encoding="utf-8"
    )
    (vault / "knowledge" / "projects" / "product-a" / "state.md").write_text(
        "# State\n", encoding="utf-8"
    )
    (vault / "knowledge" / "inbox" / "clip.pdf").write_bytes(b"%PDF-1.4\n")
    return vault


def _lint_report(tmp_path: Path, vault: Path) -> str:
    state = tmp_path / "state"
    env = {**os.environ, "LLM_WIKI_ROOT": str(vault), "LLM_WIKI_STATE_ROOT": str(state)}
    subprocess.run(
        [sys.executable, str(SCRIPTS / "lint_memory.py"), "--scope", "all"],
        check=True,
        capture_output=True,
        cwd=vault,
        env=env,
        timeout=120,
    )
    return next((state / "logs").glob("lint-*.md")).read_text(encoding="utf-8")


def _section(report: str, title: str) -> list[str] | None:
    match = re.search(rf"^## {title} \(\d+\)\n(.*?)(?=^## |\Z)", report, re.M | re.S)
    if match is None:
        return None
    return [line for line in match.group(1).splitlines() if line.startswith("- ")]


def test_bare_and_vault_relative_links_resolve_and_one_way_links_are_fine(tmp_path):
    report = _lint_report(tmp_path, _vault(tmp_path))

    assert _section(report, "Broken Wikilinks") == [
        "- [notes] knowledge/notes/alpha.md -> [[knowledge/notes/beta]]",
        "- [notes] knowledge/notes/alpha.md -> [[missing-note]]",
    ]
    assert _section(report, "Missing Backlinks") is None
