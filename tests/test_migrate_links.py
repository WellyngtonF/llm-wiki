"""The one-off link migration, driven as the owner runs it over a temporary vault.

Pages written before ADR 0003 carry backlink lines and repository-rooted
`[[knowledge/notes/x]]` links. The migration removes the first, rewrites the
second to the form Obsidian opens, and reports what still resolves to nothing
without guessing. A dry run writes nothing; an apply is one recoverable
transaction the owner can undo.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

LEDGER = (
    "## Claims\n"
    "```json\n"
    '{"claims":[{"id":"c1","links":["[[knowledge/notes/beta]]"],'
    '"text":"Alpha links to [[knowledge/notes/beta]]."}],'
    '"schema_version":"claim-ledger/v1"}\n'
    "```\n"
)
EVIDENCE = (
    "## Evidence\n"
    "- `daily:2026-01-01 sha256:" + "a" * 64 + " block:b1 bytes:0-10` — Alpha comes first.\n"
)
ALPHA = (
    "---\n"
    "type: concept\n"
    'related: "[[knowledge/notes/beta]]"\n'
    "---\n"
    "# Alpha\n"
    "\n"
    "One-sentence summary: the first note.\n"
    "\n"
    "See [[knowledge/notes/beta]], [[knowledge/notes/beta.md]],"
    " [[knowledge/notes/beta|the second note]], [[knowledge/notes/beta#Heading]],"
    " [[knowledge/notes/deeper/gamma]], [[knowledge/notes/delta]],"
    " [[knowledge/projects/product-a/state]] and [[knowledge/feedback/abc123.json]].\n"
    "Already bare: [[beta]]. Broken: [[missing-note]].\n"
    "Quoted, not a link: `[[knowledge/notes/beta]]`.\n"
    "\n"
    "```text\n"
    "[[knowledge/notes/beta]]\n"
    "```\n"
    "\n"
    + EVIDENCE
    + "\n"
    "## Related\n"
    "- [[beta]]\n"
    "- [[knowledge/notes/beta]] — links to this page.\n"
    "\n"
    + LEDGER
)
ALPHA_MIGRATED = (
    "---\n"
    "type: concept\n"
    'related: "[[beta]]"\n'
    "---\n"
    "# Alpha\n"
    "\n"
    "One-sentence summary: the first note.\n"
    "\n"
    "See [[beta]], [[beta]], [[beta|the second note]], [[beta#Heading]],"
    " [[gamma]], [[notes/delta]],"
    " [[projects/product-a/state]] and [[feedback/abc123.json]].\n"
    "Already bare: [[beta]]. Broken: [[missing-note]].\n"
    "Quoted, not a link: `[[knowledge/notes/beta]]`.\n"
    "\n"
    "```text\n"
    "[[knowledge/notes/beta]]\n"
    "```\n"
    "\n"
    + EVIDENCE
    + "\n"
    "## Related\n"
    "- [[beta]]\n"
    "\n"
    + LEDGER
)
BETA = (
    "---\ntype: concept\n---\n# Beta\n\n## Heading\n\nBody.\n"
    "\n## Related\n\n- [[knowledge/notes/alpha]] — links to this page.\n"
)
BETA_MIGRATED = "---\ntype: concept\n---\n# Beta\n\n## Heading\n\nBody.\n"
GAMMA = (
    "---\ntype: concept\n---\n# Gamma\n\nSee [[knowledge/notes/gone]].\n"
    "\n## Links\n\n- [[alpha]]\n- [[knowledge/notes/alpha]] - links to this page.\n"
    "\n## Update\n\nLater.\n"
)
GAMMA_MIGRATED = (
    "---\ntype: concept\n---\n# Gamma\n\nSee [[knowledge/notes/gone]].\n"
    "\n## Links\n\n- [[alpha]]\n"
    "\n## Update\n\nLater.\n"
)
CONTEXT = "---\ntype: project-context\n---\n# Context\n\nStart at [[knowledge/notes/alpha]].\n"
DAILY = "# 2026-01-01\n\n## [b1] Session\n\nMentioned [[knowledge/notes/beta]].\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    knowledge = vault / "knowledge"
    files = {
        "notes/alpha.md": ALPHA,
        "notes/beta.md": BETA,
        "notes/deeper/gamma.md": GAMMA,
        "notes/delta.md": "---\ntype: concept\n---\n# Delta\n",
        # A shallower file with the same name: bare `[[delta]]` would open this one.
        "delta.md": "# Another delta\n",
        "projects/product-a/state.md": "# State\n\nSee [[knowledge/notes/alpha]].\n",
        "projects/product-a/context.md": CONTEXT,
        "feedback/abc123.json": "{}\n",
        "daily/2026-01-01.md": DAILY,
        "raw/sessions/2026-01-01/s1.md": "Session [[knowledge/notes/beta]].\n",
    }
    for relative, text in files.items():
        path = knowledge / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return vault


def _snapshot(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted((vault / "knowledge").rglob("*"))
        if path.is_file()
    }


def _run(tmp_path: Path, vault: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(vault),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=vault,
        env=env,
        timeout=120,
    )


def _migrate(tmp_path: Path, vault: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return _run(tmp_path, vault, str(SCRIPTS / "migrate_links.py"), *flags)


def _page(vault: Path, relative: str) -> str:
    return (vault / "knowledge" / relative).read_bytes().decode("utf-8")


def test_dry_run_lists_every_change_and_writes_nothing(tmp_path):
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    result = _migrate(tmp_path, vault)

    assert result.returncode == 0, result.stderr
    assert _snapshot(vault) == before
    out = result.stdout
    assert "knowledge/notes/alpha.md" in out
    assert "REWRITE [[knowledge/notes/beta|the second note]] -> [[beta|the second note]]" in out
    assert "REWRITE [[knowledge/notes/delta]] -> [[notes/delta]]" in out
    assert "REMOVE BACKLINK - [[knowledge/notes/alpha]] — links to this page." in out
    assert "REMOVE EMPTY HEADING ## Related" in out
    assert "UNRESOLVED [[missing-note]]" in out
    assert "UNRESOLVED [[knowledge/notes/gone]]" in out
    assert "4 would change" in out
    assert "nothing was written" in out


def test_apply_migrates_reports_the_broken_links_and_can_be_undone(tmp_path):
    vault = _vault(tmp_path)
    before = _snapshot(vault)

    result = _migrate(tmp_path, vault, "--apply", "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["applied"] is True
    assert report["changed"] == 4
    assert _page(vault, "notes/alpha.md") == ALPHA_MIGRATED
    assert _page(vault, "notes/beta.md") == BETA_MIGRATED
    assert _page(vault, "notes/deeper/gamma.md") == GAMMA_MIGRATED
    assert _page(vault, "projects/product-a/context.md") == CONTEXT.replace(
        "[[knowledge/notes/alpha]]", "[[alpha]]"
    )
    # The ledger and evidence lines are the bytes they were.
    assert LEDGER in _page(vault, "notes/alpha.md")
    assert EVIDENCE in _page(vault, "notes/alpha.md")
    # Evidence and pages owned by another writer are not touched.
    after = _snapshot(vault)
    for untouched in (
        "knowledge/daily/2026-01-01.md",
        "knowledge/raw/sessions/2026-01-01/s1.md",
        "knowledge/projects/product-a/state.md",
    ):
        assert after[untouched] == before[untouched]
    unresolved = {
        (page["path"], item["link"]) for page in report["pages"] for item in page["unresolved"]
    }
    assert unresolved == {
        ("knowledge/notes/alpha.md", "[[missing-note]]"),
        ("knowledge/notes/deeper/gamma.md", "[[knowledge/notes/gone]]"),
    }

    rerun = _migrate(tmp_path, vault, "--apply", "--json")

    assert rerun.returncode == 0, rerun.stderr
    second = json.loads(rerun.stdout)
    assert second["changed"] == 0
    assert second["transaction_id"] is None
    assert _snapshot(vault) == after

    undo = _run(
        tmp_path,
        vault,
        str(SCRIPTS / "markdown_transaction.py"),
        "undo",
        report["transaction_id"],
    )

    assert undo.returncode == 0, undo.stderr
    assert _snapshot(vault) == before
