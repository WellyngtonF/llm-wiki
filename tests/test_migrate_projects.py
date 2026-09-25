"""The one-off projects migration, driven as the owner runs it over a temporary vault.

Before the project map, every directory an agent worked in got a flat
`knowledge/projects/<slug>/` journal: the repository itself, a subfolder of it, one
of its worktrees, a scratch directory, the home directory. The migration proposes a
map from the folders that name a real repository, and the note projects; the owner
edits both; a dry run shows what apply will do and writes nothing; apply moves the
kept journal under its project, deletes every other folder, gives notes their
project without overwriting one, and is one transaction the owner can undo.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_journal import ProjectStore  # noqa: E402
from project_map import parse_project_map  # noqa: E402
from test_project_journal import checkpoint_event  # noqa: E402

PROJECTS = Path("knowledge/projects")
MAP_PROPOSAL = PROJECTS / "project-map.proposed.md"
NOTES_PROPOSAL = PROJECTS / "note-projects.proposed.md"
DAY = "2026-01-02"


def _repository(parent: Path, name: str) -> Path:
    checkout = parent / name
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return checkout


def _worktree(checkout: Path, location: Path) -> Path:
    """What `git worktree add` leaves: a pointer file, and the admin dir it names."""
    admin = checkout / ".git" / "worktrees" / location.name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    location.mkdir(parents=True)
    (location / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    return location


def _event(worktree: Path | str, number: int) -> dict:
    event = checkpoint_event(
        f"evt-{uuid.uuid4().hex}",
        f"task:{number}:{uuid.uuid4().hex}",
        delta={"current_task": {"id": f"task-{number}", "action": "upsert", "value": f"Step {number}"}},
    )
    event["provenance"] = {**event["provenance"], "worktree": str(worktree)}
    return event


def _old_folder(vault: Path, state_root: Path, slug: str, worktree: Path | str, events: int) -> None:
    """A flat journal as the layout before the project map wrote it, checkpoints committed."""
    store = ProjectStore(vault, state_root)
    for number in range(1, events + 1):
        store.checkpoint(slug, _event(worktree, number), "agent-a")


def _entry(heading: str, *lines: str) -> str:
    return f"## {heading}\n" + "".join(f"{line}\n" for line in lines) + "\n"


def _note(slug: str, *, project: str | None = None, status: str | None = None, cites: tuple = ()) -> str:
    front = ["---", "type: concept", f'title: "{slug}"']
    if project:
        front.append(f'project: "{project}"')
    if status:
        front.append(f"status: {status}")
    front += ["confidence: medium", "source_authority: ai-derived", "---"]
    evidence = "".join(f"- `{reference}` — cited.\n" for reference in cites)
    return "\n".join(front) + f"\n# {slug}\n\nOne-sentence summary: {slug}.\n\n## Evidence\n{evidence}"


@pytest.fixture
def world(tmp_path: Path) -> dict:
    vault = tmp_path / "vault"
    state_root = tmp_path / "state"
    home = tmp_path / "home"
    (vault / "knowledge" / "notes").mkdir(parents=True)
    (vault / PROJECTS).mkdir(parents=True)
    (vault / "knowledge" / "daily").mkdir()
    (state_root / "run").mkdir(parents=True)
    home.mkdir()
    work = tmp_path / "work"
    alpha = _repository(work, "alpha")
    web = alpha / "apps" / "web"
    web.mkdir(parents=True)
    feature = _worktree(alpha, tmp_path / "worktrees" / "alpha-feature")
    beta = _repository(work, "beta")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    _old_folder(vault, state_root, "alpha", alpha, 3)
    _old_folder(vault, state_root, "web", web, 1)
    _old_folder(vault, state_root, "alpha-feature", feature, 2)
    _old_folder(vault, state_root, "beta", beta, 1)
    _old_folder(vault, state_root, "scratch", scratch, 1)
    _old_folder(vault, state_root, "home", home, 1)
    _old_folder(vault, state_root, "gone", tmp_path / "deleted-worktree", 1)
    leftover = vault / PROJECTS / "scratch" / f".state.md.{'a' * 32}.tmp"
    leftover.write_text("half a write", encoding="utf-8")

    entries = [
        _entry(
            "[10:00:00] session-end | sess-1",
            "- Trigger: `other`",
            "- Agent: `claude`",
            "- Project slug: `web`",
            f"- Project root: `{web}`",
        ),
        _entry(
            "[11:00:00] session-end | sess-2",
            "- Trigger: `other`",
            "- Agent: `claude`",
            "- Project slug: `beta`",
            f"- Project root: `{beta}`",
        ),
        "<!-- llm-wiki-operation:" + "b" * 64 + " -->\n\n"
        "- `[12:00:00] tool | claude | s3 | alpha-feature | Bash` ls\n\n",
    ]
    daily = f"# {DAY}\n\n" + "".join(entries)
    daily_bytes = daily.encode("utf-8")
    (vault / "knowledge" / "daily" / f"{DAY}.md").write_bytes(daily_bytes)
    digest = hashlib.sha256(daily_bytes).hexdigest()
    references = {}
    offset = len(f"# {DAY}\n\n".encode())
    for block, entry in zip(("10:00:00", "11:00:00", "12:00:00"), entries):
        size = len(entry.encode("utf-8"))
        references[block] = f"daily:{DAY} sha256:{digest} block:{block} bytes:{offset}-{offset + 40}"
        offset += size

    notes = {
        "cache-strategy": _note("cache-strategy", cites=(references["10:00:00"],)),
        "deploy-order": _note("deploy-order", cites=(references["11:00:00"],)),
        "crumb-note": _note("crumb-note", cites=(references["12:00:00"],)),
        "alpha-logging": _note("alpha-logging"),
        "general-tip": _note("general-tip"),
        "owned": _note("owned", project="other", cites=(references["11:00:00"],)),
        "old-news": _note("old-news", status="superseded", cites=(references["10:00:00"],)),
    }
    for slug, text in notes.items():
        (vault / "knowledge" / "notes" / f"{slug}.md").write_bytes(text.encode("utf-8"))

    pending = {"occurred_at": "2026-01-02T10:00:00+00:00", "event_id": "e1"}
    state = {
        "project_checkpoint_pending": {"scratch": [pending], "alpha": [dict(pending, event_id="e2")]},
        "project_checkpoint_reducers": {"scratch:s1": {}, "alpha:s1": {}},
        "unrelated": 1,
    }
    (state_root / "run" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        "vault": vault,
        "state_root": state_root,
        "home": home,
        "alpha": alpha,
        "beta": beta,
        "feature": feature,
    }


def _migrate(world: dict, *flags: str) -> subprocess.CompletedProcess[str]:
    return _run(world, str(SCRIPTS / "migrate_projects.py"), *flags)


def _run(world: dict, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LLM_WIKI_ROOT": str(world["vault"]),
        "LLM_WIKI_STATE_ROOT": str(world["state_root"]),
        "USERPROFILE": str(world["home"]),
        "HOME": str(world["home"]),
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=world["vault"],
        env=env,
        timeout=180,
    )


def _snapshot(world: dict, *, markdown_only: bool = False) -> dict[str, bytes]:
    vault = world["vault"]
    files = {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in sorted((vault / "knowledge").rglob("*"))
        if path.is_file() and (not markdown_only or path.suffix == ".md")
    }
    if not markdown_only:
        files["state.json"] = (world["state_root"] / "run" / "state.json").read_bytes()
    return files


def _project_files(vault: Path) -> list[str]:
    root = vault / PROJECTS
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _frontmatter_project(vault: Path, slug: str) -> str | None:
    text = (vault / "knowledge" / "notes" / f"{slug}.md").read_text(encoding="utf-8")
    lines = [line for line in text.split("---", 2)[1].splitlines() if line.startswith("project:")]
    return lines[0].split(":", 1)[1].strip().strip('"') if lines else None


def _proposed_notes(vault: Path) -> dict[str, str]:
    text = (vault / NOTES_PROPOSAL).read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        if line.startswith("- ") and ": " in line:
            slug, rest = line[2:].split(": ", 1)
            rows[slug] = rest.split(" — ", 1)[0]
    return rows


def _propose(world: dict) -> subprocess.CompletedProcess[str]:
    result = _migrate(world, "--propose")
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _approve(world: dict) -> None:
    """The owner renames one project and keeps everything else as proposed."""
    vault = world["vault"]
    proposal = (vault / MAP_PROPOSAL).read_text(encoding="utf-8")
    (vault / MAP_PROPOSAL).write_text(proposal.replace("## beta", "## product-b"), encoding="utf-8")
    notes = (vault / NOTES_PROPOSAL).read_text(encoding="utf-8")
    (vault / NOTES_PROPOSAL).write_text(
        notes.replace("- deploy-order: beta", "- deploy-order: product-b"), encoding="utf-8"
    )


def test_propose_groups_folders_by_repository_and_proposes_note_projects(world):
    vault = world["vault"]

    result = _propose(world)

    proposed = parse_project_map((vault / MAP_PROPOSAL).read_text(encoding="utf-8"))
    assert proposed.as_data() == [
        {"name": "alpha", "repositories": [world["alpha"].as_posix()]},
        {"name": "beta", "repositories": [world["beta"].as_posix()]},
    ]
    assert not (vault / "knowledge/projects/project-map.md").exists()
    assert _proposed_notes(vault) == {
        "alpha-logging": "alpha",
        "cache-strategy": "alpha",
        "crumb-note": "alpha",
        "deploy-order": "beta",
        "general-tip": "-",
        "owned": "other",
    }
    assert "Old project folders: 7, 4 in a git repository" in result.stdout
    assert "Proposed projects: 2" in result.stdout

    again = _migrate(world, "--propose")

    assert again.returncode == 1
    assert "already exists" in again.stdout


def test_apply_without_the_proposals_is_refused_and_writes_nothing(world):
    before = _snapshot(world)

    result = _migrate(world, "--apply")

    assert result.returncode == 1
    assert "run --propose first" in result.stdout
    assert _snapshot(world) == before


def test_dry_run_shows_the_map_the_folders_and_the_notes_and_writes_nothing(world):
    _propose(world)
    _approve(world)
    before = _snapshot(world)

    result = _migrate(world)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _snapshot(world) == before
    out = result.stdout
    assert "  product-b\n" in out
    assert "KEEP    alpha -> knowledge/projects/alpha/alpha/ (3 events)" in out
    assert "KEEP    beta -> knowledge/projects/product-b/beta/ (1 events)" in out
    for junk in ("web", "alpha-feature", "scratch", "home", "gone"):
        assert f"DELETE  {junk} " in out
        assert f"  knowledge/projects/{junk}/\n" in out
    assert "its repository keeps the folder with more events, alpha" in out
    assert "the home directory is not a project" in out
    assert "not inside a git repository" in out
    assert "no longer exists" in out
    assert "  deploy-order: product-b" in out
    assert "  general-tip" not in out
    assert "owned" not in out.split("Notes that get a project")[1].split("\n")[1]
    assert "Checkpoint queue keys to clear in run/state.json: scratch" in out
    assert "dry run: nothing was written" in out


def test_apply_moves_kept_journals_deletes_the_rest_and_can_be_undone(world):
    vault = world["vault"]
    _propose(world)
    _approve(world)
    before = _snapshot(world, markdown_only=True)

    result = _migrate(world, "--apply", "--json")

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["applied"] is True
    assert _project_files(vault) == [
        "alpha/alpha/journal.md",
        "alpha/alpha/state.md",
        "product-b/beta/journal.md",
        "product-b/beta/state.md",
        "project-map.md",
    ]
    assert parse_project_map((vault / PROJECTS / "project-map.md").read_text("utf-8")).as_data() == [
        {"name": "alpha", "repositories": [world["alpha"].as_posix()]},
        {"name": "product-b", "repositories": [world["beta"].as_posix()]},
    ]
    state = (vault / PROJECTS / "alpha/alpha/state.md").read_text(encoding="utf-8")
    assert 'project: "alpha"' in state and 'repository: "alpha"' in state
    assert "last_applied_sequence: 3" in state
    assert before["knowledge/projects/alpha/journal.md"] == (
        vault / PROJECTS / "alpha/alpha/journal.md"
    ).read_bytes()
    assert {slug: _frontmatter_project(vault, slug) for slug in (
        "cache-strategy", "deploy-order", "crumb-note", "alpha-logging", "general-tip", "owned", "old-news"
    )} == {
        "cache-strategy": "alpha",
        "deploy-order": "product-b",
        "crumb-note": "alpha",
        "alpha-logging": "alpha",
        "general-tip": None,
        "owned": "other",
        "old-news": None,
    }
    state_json = json.loads((world["state_root"] / "run" / "state.json").read_text("utf-8"))
    assert state_json["project_checkpoint_pending"] == {"alpha": [state_json["project_checkpoint_pending"]["alpha"][0]]}
    assert state_json["project_checkpoint_reducers"] == {"alpha:s1": {}}
    assert state_json["unrelated"] == 1
    assert report["keys_cleared"] == ["scratch"]

    after = _snapshot(world)
    rerun = _migrate(world, "--apply")

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "nothing to migrate" in rerun.stdout
    assert _snapshot(world) == after

    undo = _run(world, str(SCRIPTS / "markdown_transaction.py"), "undo", report["transaction_id"])

    assert undo.returncode == 0, undo.stdout + undo.stderr
    assert _snapshot(world, markdown_only=True) == before

    again = _migrate(world, "--apply")

    assert again.returncode == 0, again.stdout + again.stderr
    assert _snapshot(world, markdown_only=True) == {
        path: content for path, content in after.items() if path.endswith(".md")
    }


def test_the_moved_journal_keeps_its_sequence_for_a_worktree_of_its_repository(world, monkeypatch):
    from work_state import placement_of, work_state_store

    vault, state_root = world["vault"], world["state_root"]
    _propose(world)
    _approve(world)
    assert _migrate(world, "--apply").returncode == 0

    placement = placement_of(vault, world["feature"])
    store, key = work_state_store(vault, state_root, placement)
    store.checkpoint(key, _event(world["feature"], 4), "agent-b")

    assert placement.relative == "alpha/alpha"
    assert key == "alpha"
    journal = (vault / PROJECTS / "alpha/alpha/journal.md").read_text(encoding="utf-8")
    sequences = [json.loads(line)["sequence"] for line in journal.splitlines() if line.startswith("{")]
    assert sequences == [1, 2, 3, 4]
