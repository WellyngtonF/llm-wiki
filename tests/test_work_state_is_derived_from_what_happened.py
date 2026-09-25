"""Work state records failures, skips turns that changed nothing, and names the branch.

Measured on the owner's vault: 46 tool-failure events opened no blocker, because the
blocker was derived only from an envelope severity that no host hook sets; every
empty Codex turn appended a checkpoint, because `session_end` bypasses the reducer's
throttle whatever it carries; and all 1 976 journal events named the branch
`unknown`, because no hook passes one. Stage 2 of
`docs/specs/2026-09-24-readable-memory.md`.

Every test here drives the adapter as a host hook does and reads the files a person
or the next agent reads: `journal.md` and `state.md`.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import integration_adapter  # noqa: E402


@pytest.fixture
def vault(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "knowledge" / "projects").mkdir(parents=True)
    monkeypatch.setattr(integration_adapter, "ROOT", root)
    monkeypatch.setattr(integration_adapter, "STATE_ROOT", state_root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setattr(integration_adapter, "spawn_detached", lambda args: None)
    return root


def _repository(tmp_path: Path, head: str = "ref: refs/heads/main\n") -> Path:
    """A checkout with its own `.git/HEAD`; the name is unique per test run."""
    repository = tmp_path / f"product-a-{uuid.uuid4().hex[:8]}"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "HEAD").write_text(head, encoding="utf-8")
    return repository


def _hook(argv: list[str], raw: dict) -> None:
    with patch.object(sys, "stdin", io.StringIO(json.dumps(raw))):
        assert integration_adapter.main(argv) == 0


def _claude(event: str, raw: dict, *extra: str) -> None:
    _hook(["--source", "claude", "--event", event, *extra], raw)


def _project_files(vault: Path) -> list[Path]:
    return sorted((vault / "knowledge" / "projects").glob("*/journal.md"))


def _journal_events(vault: Path) -> list[dict]:
    journals = _project_files(vault)
    assert len(journals) <= 1, journals
    if not journals:
        return []
    lines = journals[0].read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


def _state(vault: Path) -> str:
    [journal] = _project_files(vault)
    return (journal.parent / "state.md").read_text(encoding="utf-8")


def _section(state: str, title: str) -> str:
    return state.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def _session() -> str:
    return f"s-{uuid.uuid4().hex[:12]}"


# --- failure events open blockers --------------------------------------------


def test_the_claude_failure_hook_opens_a_blocker(vault, tmp_path) -> None:
    """`PostToolUseFailure` passes `--checkpoint-type significant_failure`, never a severity."""
    repository = _repository(tmp_path)
    _claude(
        "post_tool_use",
        {
            "session_id": _session(),
            "cwd": str(repository),
            "tool_name": "Bash",
            "tool_input": {"command": "uv run pytest -q"},
            "tool_use_id": "toolu_1",
            "error": "Exit code 1",
        },
        "--checkpoint-type",
        "significant_failure",
    )

    blockers = _section(_state(vault), "Open blockers")

    assert "Bash failed: uv run pytest -q" in blockers


def test_an_opencode_failed_command_opens_a_blocker(vault, tmp_path) -> None:
    """What the OpenCode plugin forwards when a shell command exits non-zero."""
    repository = _repository(tmp_path)
    _hook(
        ["--source", "opencode", "--event", "post_tool_use"],
        {
            "tool": "bash",
            "sessionID": _session(),
            "callID": "call-1",
            "args": {"command": "npm test"},
            "directory": str(repository),
            "significant_failure": True,
            "checkpoint_type": "significant_failure",
        },
    )

    assert "Bash failed: npm test" in _section(_state(vault), "Open blockers")


def test_a_successful_tool_opens_no_blocker(vault, tmp_path) -> None:
    repository = _repository(tmp_path)
    _claude(
        "post_tool_use",
        {
            "session_id": _session(),
            "cwd": str(repository),
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/app.py"},
        },
    )

    state = _state(vault)

    assert _section(state, "Open blockers").strip() == "- None"
    assert "src/app.py" in _section(state, "Changed files")


def _opencode_forwarded(plugin: Path, tool: str, output: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    script = textwrap.dedent(
        f"""
        process.env.LLM_WIKI_ROOT = "/vault";
        const payloads = [];
        globalThis.Bun = {{ spawn() {{
          let finish;
          const exited = new Promise((resolve) => {{ finish = resolve; }});
          return {{
            stdin: {{ write(value) {{ payloads.push(JSON.parse(value)); }}, end() {{ finish(0); }} }},
            stdout: new ReadableStream({{ start(controller) {{ controller.close(); }} }}),
            exited,
            kill() {{ finish(143); }},
          }};
        }} }};
        const {{ LlmWikiMemoryPlugin }} = await import({json.dumps(plugin.resolve().as_uri())});
        const hooks = await LlmWikiMemoryPlugin({{ client: {{}}, directory: "/work/product-a" }});
        await hooks["tool.execute.after"](
          {{ sessionID: "s1", tool: {json.dumps(tool)}, callID: "c1", args: {{ command: "npm test" }} }},
          {json.dumps(output)},
        );
        console.log(JSON.stringify(payloads));
        """
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    [forwarded] = json.loads(result.stdout)
    return forwarded


@pytest.fixture
def opencode_plugin(tmp_path) -> Path:
    plugin = tmp_path / "llm-wiki-memory-opencode.mjs"
    shutil.copyfile(SCRIPTS_DIR / "llm-wiki-memory-opencode.js", plugin)
    return plugin


def test_the_opencode_plugin_forwards_a_non_zero_exit_as_a_failure(opencode_plugin) -> None:
    forwarded = _opencode_forwarded(
        opencode_plugin, "bash", {"title": "npm test", "output": "1 failed", "metadata": {"exit": 1}}
    )

    assert (forwarded["significant_failure"], forwarded["checkpoint_type"]) == (
        True,
        "significant_failure",
    )


def test_the_opencode_plugin_forwards_a_zero_exit_as_no_failure(opencode_plugin) -> None:
    forwarded = _opencode_forwarded(
        opencode_plugin, "bash", {"title": "npm test", "output": "ok", "metadata": {"exit": 0}}
    )

    assert "significant_failure" not in forwarded
    assert "checkpoint_type" not in forwarded


# --- turns that change nothing append nothing ---------------------------------


def test_a_turn_that_changed_nothing_appends_no_checkpoint(vault, tmp_path) -> None:
    """An empty Codex turn ends as `session_end`, which bypasses every throttle."""
    repository = _repository(tmp_path)
    session = _session()

    for turn in range(3):
        _hook(
            ["--source", "codex", "--event", "session_end"],
            {"session_id": session, "cwd": str(repository), "event_id": f"turn-{turn}"},
        )

    assert _journal_events(vault) == []


def test_after_real_work_an_empty_turn_adds_nothing_and_sequences_stay_contiguous(
    vault, tmp_path
) -> None:
    repository = _repository(tmp_path)
    session = _session()
    edit = {
        "session_id": session,
        "cwd": str(repository),
        "tool_name": "Edit",
        "tool_input": {"file_path": "src/app.py"},
    }

    _claude("post_tool_use", {**edit, "tool_use_id": "toolu_1"})
    _claude("session_end", {"session_id": session, "cwd": str(repository)})
    _claude(
        "post_tool_use",
        {**edit, "tool_use_id": "toolu_2", "tool_name": "Bash", "tool_input": {"command": "false"}},
        "--checkpoint-type",
        "significant_failure",
    )

    events = _journal_events(vault)

    assert [event["sequence"] for event in events] == [1, 2]
    assert [event["trigger"] for event in events] == ["file_changed", "significant_failure"]


def test_a_stated_close_is_a_change_and_is_appended(vault, tmp_path) -> None:
    """Closing an open task changes the work state, so it is not an empty turn."""
    repository = _repository(tmp_path)
    session = _session()
    opened = integration_adapter._empty_delta()
    opened["current_task"] = {"id": "task-1", "action": "upsert", "value": "Ship login"}
    closed = integration_adapter._empty_delta()
    closed["current_task"] = {"id": "task-1", "action": "close", "value": "done"}

    _claude(
        "session_end",
        {"session_id": session, "cwd": str(repository), "project_delta": opened},
    )
    assert "Ship login" in _section(_state(vault), "Current task")
    _claude(
        "session_end",
        {"session_id": _session(), "cwd": str(repository), "project_delta": closed},
    )

    assert len(_journal_events(vault)) == 2
    assert _section(_state(vault), "Current task").strip() == "- None"


# --- the branch is read from the repository -----------------------------------


def _edit_in(directory: Path) -> None:
    _claude(
        "post_tool_use",
        {
            "session_id": _session(),
            "cwd": str(directory),
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/app.py"},
        },
    )


def _recorded_branch(vault: Path) -> str:
    [event] = _journal_events(vault)
    return event["provenance"]["branch"]


def test_the_branch_is_read_from_the_checkout(vault, tmp_path) -> None:
    _edit_in(_repository(tmp_path, "ref: refs/heads/feature/login\n"))

    assert _recorded_branch(vault) == "feature/login"


def test_a_subfolder_reads_the_branch_of_its_repository(vault, tmp_path) -> None:
    repository = _repository(tmp_path, "ref: refs/heads/backend-work\n")
    subfolder = repository / "services" / "api"
    subfolder.mkdir(parents=True)

    _edit_in(subfolder)

    assert _recorded_branch(vault) == "backend-work"


def test_a_worktree_reads_its_own_branch_through_the_pointer_file(vault, tmp_path) -> None:
    """In a worktree `.git` is a file naming the worktree's own git directory."""
    main = _repository(tmp_path, "ref: refs/heads/main\n")
    gitdir = main / ".git" / "worktrees" / "review"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/review-fix\n", encoding="utf-8")
    worktree = tmp_path / f"product-a-review-{uuid.uuid4().hex[:8]}"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    _edit_in(worktree)

    assert _recorded_branch(vault) == "review-fix"


def test_a_detached_head_is_named_by_its_commit(vault, tmp_path) -> None:
    _edit_in(_repository(tmp_path, "0123456789abcdef0123456789abcdef01234567\n"))

    assert _recorded_branch(vault) == "detached at 0123456789ab"


def test_a_directory_outside_any_repository_records_an_unknown_branch(vault, tmp_path) -> None:
    plain = tmp_path / f"notes-{uuid.uuid4().hex[:8]}"
    plain.mkdir()

    _edit_in(plain)

    assert _recorded_branch(vault) == "unknown"


def test_a_branch_the_host_states_wins(vault, tmp_path) -> None:
    repository = _repository(tmp_path, "ref: refs/heads/main\n")
    _claude(
        "post_tool_use",
        {
            "session_id": _session(),
            "cwd": str(repository),
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/app.py"},
            "branch": "release-2",
        },
    )

    assert _recorded_branch(vault) == "release-2"
