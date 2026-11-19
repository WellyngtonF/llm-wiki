"""Antigravity is a supported host again (ADR 0010): hooks, transcript, install, doctor."""
from __future__ import annotations

import importlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
T3_NAME = "ab" * 32
UV = Path("/opt/uv/bin/uv")


def _hook():
    return importlib.import_module("antigravity_hook")


def _payload(event: str, **extra: object) -> dict[str, object]:
    return {
        "hookEventName": event,
        "conversationId": "conv-1",
        "cwd": "/home/me/.t3/userdata/attachments",
        "workspacePaths": ["/home/me/.t3/userdata/attachments", "/work/repo"],
        "transcriptPath": "/g/antigravity-acp/brain/conv-1/transcript_full.jsonl",
        **extra,
    }


@pytest.fixture
def repositories(monkeypatch: pytest.MonkeyPatch):
    """Only `/work/repo` and `/work/other` are repositories."""
    known = {"/work/repo", "/work/other"}
    monkeypatch.setattr(_hook(), "_is_repository", lambda name: name in known)
    return known


def test_session_start_takes_the_first_workspace_that_is_a_repository(repositories) -> None:
    host_event, event, projected = _hook().project(_payload("SessionStart"))

    assert (host_event, event) == ("SessionStart", "session_start")
    assert projected == {
        "session_id": "conv-1",
        "transcript_path": "/g/antigravity-acp/brain/conv-1/transcript_full.jsonl",
        "cwd": "/work/repo",
        "reason": "startup",
    }


def test_a_folder_outside_git_is_not_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3's attachments folder is `cwd` and is no repository; the git workspace wins."""
    project_state = importlib.import_module("session_start_project_state")
    monkeypatch.setattr(project_state, "working_repository", lambda directory, _projects: directory)
    attachments = tmp_path / "attachments"
    repository = tmp_path / "repo"
    attachments.mkdir()
    (repository / ".git").mkdir(parents=True)
    raw = {**_payload("Stop"), "cwd": str(attachments)}
    raw["workspacePaths"] = [str(attachments), str(repository)]

    assert _hook().worktree(raw) == str(repository)


def test_without_a_repository_the_cwd_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_hook(), "_is_repository", lambda _name: False)

    assert _hook().worktree(_payload("Stop")) == "/home/me/.t3/userdata/attachments"


def test_a_command_breadcrumb_prefers_the_command_cwd(repositories) -> None:
    raw = _payload(
        "PostToolUse",
        toolCall={
            "id": "call-7",
            "name": "run_command",
            "args": {"CommandLine": "pytest -q", "Cwd": "/work/other"},
        },
        error="",
    )

    _host, event, projected = _hook().project(raw)

    assert event == "post_tool_use"
    assert projected["cwd"] == "/work/other"
    assert projected["tool_name"] == "Bash"
    assert projected["tool_input"] == {"file_path": "pytest -q"}
    assert projected["tool_use_id"] == "call-7"
    assert "checkpoint_type" not in projected


@pytest.mark.parametrize(
    ("name", "field", "shared"),
    [
        ("write_file", "Path", "Write"),
        ("edit_file", "Path", "Edit"),
        ("write_to_file", "TargetFile", "Write"),
        ("replace_file_content", "TargetFile", "Edit"),
        ("multi_replace_file_content", "TargetFile", "MultiEdit"),
    ],
)
def test_a_file_write_names_its_file(repositories, name: str, field: str, shared: str) -> None:
    raw = _payload(
        "PostToolUse", toolCall={"name": name, "args": {field: "/work/repo/a.py"}}
    )

    projected = _hook().project(raw)[2]

    assert projected["tool_name"] == shared
    assert projected["tool_input"] == {"file_path": "/work/repo/a.py"}


def test_a_failed_tool_is_a_significant_failure(repositories) -> None:
    raw = _payload(
        "PostToolUse",
        toolCall={"name": "run_command", "args": {"CommandLine": "false"}},
        error="exit status 1",
    )

    assert _hook().project(raw)[2]["checkpoint_type"] == "significant_failure"


@pytest.mark.parametrize(
    "raw",
    [
        _payload("PreInvocation"),
        _payload("PostToolUse", toolCall={"name": "read_file", "args": {"Path": "/a"}}),
        _payload("PostToolUse", toolCall={"name": "run_command", "args": {}}),
        {"hookEventName": "SessionStart"},
        _payload("Stop", workspacePaths="/work/repo"),
    ],
)
def test_an_event_it_does_not_know_is_refused(repositories, raw) -> None:
    with pytest.raises(ValueError, match="invalid Antigravity hook input"):
        _hook().project(raw)


def test_the_stop_reason_is_its_termination_reason(repositories) -> None:
    projected = _hook().project(_payload("Stop", terminationReason="completed"))[2]

    assert projected["reason"] == "completed"
    assert projected["cwd"] == "/work/repo"


def test_the_context_goes_back_as_an_injected_system_message() -> None:
    hook = _hook()

    assert hook.session_start_output({"context": "## Memory"}) == {
        "injectSteps": [{"systemMessage": {"systemMessage": "## Memory"}}]
    }
    assert hook.session_start_output({"context": ""}) == {}
    assert hook.session_start_output({}) == {}


def _run_main(monkeypatch: pytest.MonkeyPatch, capsys, raw: object) -> str:
    text = raw if isinstance(raw, str) else json.dumps(raw)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert _hook().main() == 0
    return capsys.readouterr().out


def test_main_prints_the_context_for_a_session_start(
    monkeypatch: pytest.MonkeyPatch, capsys, repositories
) -> None:
    seen: list[tuple[str, dict]] = []

    def ingest(event, projected, _started):
        seen.append((event, dict(projected)))
        return {"context": "ctx"}

    monkeypatch.setattr(_hook(), "_ingest", ingest)

    out = _run_main(monkeypatch, capsys, _payload("SessionStart"))

    assert json.loads(out) == {"injectSteps": [{"systemMessage": {"systemMessage": "ctx"}}]}
    assert seen == [("session_start", _hook().project(_payload("SessionStart"))[2])]


def test_main_never_breaks_the_host(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    failures: list[BaseException] = []
    monkeypatch.setattr(_hook(), "_record_failure", lambda error, *_: failures.append(error))

    out = _run_main(monkeypatch, capsys, "not json")

    assert json.loads(out) == {}
    assert len(failures) == 1


def test_a_turn_end_captures_once_per_window_under_its_own_key() -> None:
    turn_end = importlib.import_module("codex_turn_end")
    key = turn_end.ANTIGRAVITY_STATE_KEY
    state: dict = {}
    start = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    raw = {"session_id": "conv-1", "cwd": "/work/repo", "transcript_path": "/t", "turn_id": None}

    assert turn_end.claim_turn_end(state, raw, start, key=key) is True
    assert turn_end.claim_turn_end(state, raw, start + timedelta(minutes=5), key=key) is False
    assert turn_end.STATE_KEY not in state
    assert key in state

    later = start + timedelta(minutes=5 + 31)
    tail = turn_end.claim_quiet_tail(state, later, except_session="conv-2", key=key)
    assert tail is not None
    assert tail["session_id"] == "conv-1"
    assert turn_end.claim_quiet_tail(state, later, except_session="conv-2", key=key) is None


def test_the_antigravity_transcript_renders_as_conversation() -> None:
    evidence = importlib.import_module("session_evidence")
    lines = [
        {"step_index": 0, "source": "USER", "type": "USER_INPUT", "content": "list the files"},
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "content": "Listing them.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls -la"}}],
        },
        {"step_index": 2, "source": "SYSTEM", "type": "GENERIC", "content": "internal"},
    ]

    rendered = evidence.render_transcript("\n".join(json.dumps(line) for line in lines))

    assert "list the files" in rendered
    assert "Listing them." in rendered
    assert "ls -la" in rendered
    assert "internal" not in rendered


def test_the_brains_of_every_installation_are_transcript_roots(tmp_path: Path) -> None:
    transcripts = importlib.import_module("host_transcripts")
    home = tmp_path / "home"
    t3 = home / ".t3" / "userdata" / "providers" / "antigravity" / T3_NAME
    t3.mkdir(parents=True)

    roots = transcripts.host_transcript_roots({}, home)

    assert t3 / "antigravity-acp" / "brain" in roots
    assert home / ".gemini" / "antigravity" / "brain" in roots


# --- installation -----------------------------------------------------------


def _install():
    return importlib.import_module("antigravity_install")


def _t3_dir(home: Path) -> Path:
    path = home / ".t3" / "userdata" / "providers" / "antigravity" / T3_NAME
    path.mkdir(parents=True)
    return path


def test_installations_are_the_standalone_gemini_dir_and_each_t3_instance(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert _install().installations(home) == []

    (home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    t3 = _t3_dir(home)

    assert _install().installations(home) == [home / ".gemini", t3]


def test_a_gemini_dir_without_antigravity_is_not_an_installation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)

    assert _install().installations(home) == []


def test_configure_mcp_adds_the_entry_and_keeps_other_servers(tmp_path: Path) -> None:
    install = _install()
    gemini = _t3_dir(tmp_path / "home")
    config = install.mcp_config_path(gemini)
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    vault = tmp_path / "vault"

    assert install.configure_mcp(gemini, vault, UV) == "current"

    servers = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["other"] == {"command": "x"}
    assert servers["llm-wiki"] == install.expected_entry(vault, UV)
    assert install.mcp_state(gemini, vault) == "current"
    assert install.configure_mcp(gemini, vault, UV) == "current"


def test_configure_mcp_never_replaces_another_entry(tmp_path: Path) -> None:
    install = _install()
    gemini = _t3_dir(tmp_path / "home")
    config = install.mcp_config_path(gemini)
    config.parent.mkdir(parents=True)
    theirs = {"mcpServers": {"llm-wiki": {"command": "node", "args": ["elsewhere.js"]}}}
    config.write_text(json.dumps(theirs), encoding="utf-8")

    assert install.configure_mcp(gemini, tmp_path / "vault", UV) == "elsewhere"
    assert json.loads(config.read_text(encoding="utf-8")) == theirs


def test_an_unreadable_mcp_config_is_invalid_and_left_alone(tmp_path: Path) -> None:
    install = _install()
    gemini = _t3_dir(tmp_path / "home")
    config = install.mcp_config_path(gemini)
    config.parent.mkdir(parents=True)
    config.write_text("[1, 2]", encoding="utf-8")

    assert install.configure_mcp(gemini, tmp_path / "vault", UV) == "invalid"
    assert config.read_text(encoding="utf-8") == "[1, 2]"


def test_configure_all_reports_each_installation(tmp_path: Path) -> None:
    install = _install()
    home = tmp_path / "home"
    assert install.configure_all(home, tmp_path / "vault", UV)["status"] == "not_detected"

    gemini = _t3_dir(home)
    result = install.configure_all(home, tmp_path / "vault", UV)

    assert result == {
        "status": "active",
        "installations": [{"path": str(gemini), "mcp": "current"}],
    }


# --- hooks resource ---------------------------------------------------------


def _hooks():
    return importlib.import_module("integration_hook_config")


def _vault(parent: Path, name: str = "vault") -> Path:
    """A vault holding the shipped hooks template, which the resource reads."""
    vault = parent / name
    template = vault / "integrations" / "antigravity" / "hooks.json"
    template.parent.mkdir(parents=True)
    template.write_bytes((ROOT / "integrations" / "antigravity" / "hooks.json").read_bytes())
    return vault


def test_the_template_names_this_vault_and_this_uv(tmp_path: Path) -> None:
    vault = _vault(tmp_path, "my vault")
    owned = _hooks().antigravity_hooks_template(vault, UV)
    text = json.dumps(owned)

    assert "__UV__" not in text and "__LLM_WIKI_ROOT__" not in text
    command = owned["SessionStart"][0]["command"]
    assert command.startswith(f'"{UV}" run --locked --no-sync --directory "{vault}"')
    assert command.endswith(f'"{vault}/scripts/antigravity_hook.py"')
    assert set(owned) == {"SessionStart", "PostToolUse", "Stop"}


def test_the_resource_ids_name_their_gemini_dirs(tmp_path: Path) -> None:
    hooks = _hooks()
    home = tmp_path / "home"

    assert hooks.antigravity_gemini_dir(home, "antigravity-user-hooks") == home / ".gemini"
    assert hooks.antigravity_gemini_dir(home, f"antigravity-t3-hooks-{T3_NAME}") == (
        home / ".t3" / "userdata" / "providers" / "antigravity" / T3_NAME
    )
    for unknown in ("antigravity-t3-hooks-../../etc", "antigravity-t3-hooks-", "cursor-user-hooks"):
        with pytest.raises(Exception, match="install_resource_unknown"):
            hooks.antigravity_gemini_dir(home, unknown)


def _release() -> dict[str, object]:
    return {
        "commit_oid": "a" * 40,
        "project_version": "4.0.0",
        "source_mode": "pinned_remote",
        "uv_lock_sha256": "b" * 64,
        "worktree_clean": True,
    }


def test_install_and_uninstall_keep_the_users_own_hooks(tmp_path: Path) -> None:
    hooks = _hooks()
    install_control = importlib.import_module("install_control")
    home = tmp_path / "home"
    gemini = _t3_dir(home)
    destination = gemini / "config" / "hooks.json"
    destination.parent.mkdir(parents=True)
    theirs = {"mine": {"Stop": [{"type": "command", "command": "say done"}]}}
    destination.write_text(json.dumps(theirs), encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    owned = hooks.antigravity_hooks_template(_vault(tmp_path), UV)

    def resource():
        return hooks.antigravity_hooks_resource(f"antigravity-t3-hooks-{T3_NAME}", home, owned)

    install_control.install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource()],
        control_version=2,
    )
    installed = json.loads(destination.read_text(encoding="utf-8"))
    assert installed["mine"] == theirs["mine"]
    assert installed["llm-wiki"] == owned

    install_control.uninstall_resources(state_root=state_root, resources=[resource()])

    assert json.loads(destination.read_text(encoding="utf-8")) == theirs


def test_an_install_without_a_hooks_file_leaves_none_behind(tmp_path: Path) -> None:
    hooks = _hooks()
    install_control = importlib.import_module("install_control")
    home = tmp_path / "home"
    destination = _t3_dir(home) / "config" / "hooks.json"
    state_root = tmp_path / "state"
    state_root.mkdir()
    owned = hooks.antigravity_hooks_template(_vault(tmp_path), UV)

    def resource():
        return hooks.antigravity_hooks_resource(f"antigravity-t3-hooks-{T3_NAME}", home, owned)

    install_control.install_resources(
        state_root=state_root,
        vault_root=tmp_path / "vault",
        release=_release(),
        scheduler_backend="cron",
        resources=[resource()],
        control_version=2,
    )
    assert destination.is_file()

    install_control.uninstall_resources(state_root=state_root, resources=[resource()])

    assert not destination.exists()


def test_an_uninstall_names_the_antigravity_mcp_entry_it_leaves(tmp_path: Path) -> None:
    install_control = importlib.import_module("install_control")
    home = tmp_path / "home"
    gemini = _t3_dir(home)
    _install().configure_mcp(gemini, tmp_path / "vault", UV)

    left = [
        item
        for item in install_control.unowned_agent_registrations(home)
        if item["agent"] == "antigravity"
    ]

    assert left == [
        {
            "agent": "antigravity",
            "path": str(gemini / "config" / "mcp_config.json"),
            "remove_with": 'delete "llm-wiki" under "mcpServers"',
        }
    ]


# --- doctor -----------------------------------------------------------------


def test_doctor_reports_each_antigravity_installation(tmp_path: Path) -> None:
    doctor = importlib.import_module("doctor")
    home = tmp_path / "home"
    vault = _vault(tmp_path)
    assert doctor._antigravity_host_result(vault, home)["status"] == "skipped"

    gemini = _t3_dir(home)
    degraded = doctor._antigravity_host_result(vault, home)
    assert degraded["status"] == "degraded"
    assert degraded["installations"] == [str(gemini)]

    _install().configure_mcp(gemini, vault, UV)
    owned = _hooks().antigravity_hooks_template(vault, UV)
    (gemini / "config" / "hooks.json").write_text(
        json.dumps({"llm-wiki": owned}), encoding="utf-8"
    )

    assert doctor._antigravity_host_result(vault, home)["status"] == "ok"


def test_each_installation_gets_its_own_resource_id(tmp_path: Path) -> None:
    hooks = _hooks()
    home = tmp_path / "home"
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    _t3_dir(home)

    ids = hooks.antigravity_hooks_ids(home)

    assert ids == ("antigravity-user-hooks", f"antigravity-t3-hooks-{T3_NAME}")
    assert all(hooks.is_antigravity_hooks_id(name) for name in ids)
    assert not hooks.is_antigravity_hooks_id("cursor-user-hooks")
