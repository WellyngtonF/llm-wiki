r"""Antigravity lifecycle hooks: one entry point for every event it fires.

Antigravity reads `hooks.json` from its gemini directory's `config/` folder and
runs the command with the event as camelCase JSON on stdin; the payload names its
own event in `hookEventName`. Three events matter here (ADR 0010):

- `SessionStart` — once per conversation; the bounded context goes back as an
  injected system message.
- `PostToolUse` — a command or a file write leaves a breadcrumb.
- `Stop` — the end of a turn. Antigravity fires it after every turn and has no
  session-end event, so a turn end captures the conversation only when it was
  not captured within the window; otherwise it is remembered as a tail and the
  next Antigravity hook captures it once it has been quiet for the window
  (`codex_turn_end`, the same rule Codex uses).

The adapter never breaks the host: a failure prints `{}` and exits 0.
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_turn_end import TURN_END_LOCK_SECONDS  # noqa: E402
from integration_adapter import (  # noqa: E402
    hook_deadline,
    ingest_event,
    normalize_occurrence_event,
)
from memory_state import ROOT, update_state  # noqa: E402

MAX_HOOK_INPUT_BYTES = 256 * 1024
MAX_FIELD_CHARS = 16_384
MAX_WORKSPACES = 16
# Antigravity's tool name -> (the shared tool name, the argument naming its target).
# `write_file`/`edit_file` are what the agent has today; the older names are what
# its IDE documents, kept so either build is read.
TOOLS = {
    "run_command": ("Bash", "CommandLine"),
    "write_file": ("Write", "Path"),
    "edit_file": ("Edit", "Path"),
    "write_to_file": ("Write", "TargetFile"),
    "replace_file_content": ("Edit", "TargetFile"),
    "multi_replace_file_content": ("MultiEdit", "TargetFile"),
}


def _invalid() -> ValueError:
    return ValueError("invalid Antigravity hook input")


def _text(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise _invalid()
    return value


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid()
    return value


def _workspaces(raw: Mapping[str, Any]) -> list[str]:
    value = raw.get("workspacePaths")
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_WORKSPACES:
        raise _invalid()
    return [str(_text(item, required=True)) for item in value]


def _is_repository(directory: str) -> bool:
    """A git checkout an agent may work in.

    `working_repository` answers the directory itself when no `.git` is above it,
    so a plain folder such as T3's attachments would pass without the marker check.
    """
    from session_start_project_state import working_repository

    try:
        checkout = working_repository(Path(directory), ROOT / "knowledge" / "projects")
        return (checkout / ".git").exists()
    except (OSError, ValueError):
        return False


def worktree(raw: Mapping[str, Any], preferred: str | None = None) -> str | None:
    """The directory the work happened in.

    `cwd` is the agent's first workspace, and inside T3 Code that is T3's own
    attachments folder, not the project, so the first candidate that is a
    repository wins; with none, `cwd` stands.
    """
    cwd = _text(raw.get("cwd"))
    candidates = [name for name in (preferred, cwd, *_workspaces(raw)) if name]
    return next((name for name in candidates if _is_repository(name)), cwd)


def _common(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": _text(raw.get("conversationId"), required=True),
        "transcript_path": _text(raw.get("transcriptPath")),
    }


def _session_start(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {**_common(raw), "cwd": worktree(raw), "reason": "startup"}


def _post_tool(raw: Mapping[str, Any]) -> dict[str, Any]:
    call = _mapping(raw.get("toolCall"))
    spec = TOOLS.get(str(_text(call.get("name"), required=True)))
    if spec is None:
        raise _invalid()
    name, target_field = spec
    args = _mapping(call.get("args"))
    projected = {
        **_common(raw),
        "cwd": worktree(raw, _text(args.get("Cwd"))),
        "tool_name": name,
        "tool_input": {"file_path": _text(args.get(target_field), required=True)},
    }
    call_id = _text(call.get("id"))
    if call_id:
        projected["tool_use_id"] = call_id
    if _text(raw.get("error")):
        projected["checkpoint_type"] = "significant_failure"
    return projected


def _stop(raw: Mapping[str, Any]) -> dict[str, Any]:
    reason = _text(raw.get("terminationReason")) or "stop"
    return {**_common(raw), "cwd": worktree(raw), "reason": reason}


EVENTS = {
    "SessionStart": ("session_start", _session_start),
    "PostToolUse": ("post_tool_use", _post_tool),
    "Stop": ("session_end", _stop),
}


def project(raw: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """(host event, shared event, the shared payload) of one Antigravity hook."""
    host_event = _text(raw.get("hookEventName"), required=True)
    known = EVENTS.get(str(host_event))
    if known is None:
        raise _invalid()
    event, build = known
    return str(host_event), event, build(raw)


def _read_input() -> dict[str, Any]:
    text = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    if not text or len(text.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise _invalid()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise _invalid()
    return value


def _turn_end_plan(projected: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Whether this turn end is a capture, and one other session's quiet tail."""
    import codex_turn_end

    key = codex_turn_end.ANTIGRAVITY_STATE_KEY
    session_id = str(projected["session_id"])
    plan: dict[str, Any] = {"capture": True, "claimed": False, "before": None, "tail": None}
    raw = {**projected, "turn_id": None}

    def mutate(state: dict[str, Any]) -> None:
        plan["before"] = codex_turn_end.snapshot(state, session_id, key=key)
        plan["capture"] = codex_turn_end.claim_turn_end(state, raw, now, key=key)
        plan["tail"] = codex_turn_end.claim_quiet_tail(
            state, now, except_session=session_id, key=key
        )
        plan["claimed"] = True

    try:
        update_state(mutate, lock_timeout=TURN_END_LOCK_SECONDS)
    except OSError:
        return {"capture": True, "claimed": False, "before": None, "tail": None}
    return plan


def _restore(session_id: str, before: object) -> None:
    """The capture this claim stood for failed, so the next turn end may try again."""
    import codex_turn_end

    key = codex_turn_end.ANTIGRAVITY_STATE_KEY
    try:
        update_state(
            lambda state: codex_turn_end.restore(state, session_id, before, key=key),
            lock_timeout=TURN_END_LOCK_SECONDS,
        )
    except OSError:
        return


def _ingest(event: str, projected: Mapping[str, Any], started: float) -> dict[str, Any]:
    envelope = normalize_occurrence_event("antigravity", event, projected)
    return ingest_event(
        envelope, trigger="antigravity-hook", deadline=hook_deadline(event, started)
    )


def _ingest_turn_end(projected: Mapping[str, Any], started: float) -> dict[str, Any]:
    plan = _turn_end_plan(projected, datetime.now(timezone.utc))
    event = "session_end" if plan["capture"] else "stop"
    try:
        result = _ingest(event, projected, started)
    except Exception:
        if plan["claimed"]:
            _restore(str(projected["session_id"]), plan["before"])
        raise
    return {"result": result, "tail": plan["tail"]}


def _capture_tail(tail: Mapping[str, Any] | None, started: float) -> None:
    """Capture the end of a conversation that went quiet; its failure is its own."""
    if tail is None:
        return
    projected = {
        "session_id": tail["session_id"],
        "cwd": tail.get("cwd"),
        "transcript_path": tail.get("transcript_path"),
        "reason": "stop",
    }
    try:
        _ingest("session_end", projected, started)
    except Exception as error:  # noqa: BLE001 - recorded; the hook's own answer stands
        _restore(str(tail["session_id"]), tail.get("before"))
        _record_failure(error, str(tail["session_id"]))


def session_start_output(result: Mapping[str, Any]) -> dict[str, Any]:
    """The context, as the system message Antigravity injects into the conversation."""
    context = result.get("context")
    if not isinstance(context, str) or not context:
        return {}
    return {"injectSteps": [{"systemMessage": {"systemMessage": context}}]}


def _dispatch() -> None:
    started = time.monotonic()
    host_event, event, projected = project(_read_input())
    if host_event != "Stop":
        result = _ingest(event, projected, started)
        output = session_start_output(result) if host_event == "SessionStart" else {}
        print(json.dumps(output, ensure_ascii=False))
        return
    turn = _ingest_turn_end(projected, started)
    print("{}")
    sys.stdout.flush()
    _capture_tail(turn["tail"], started)


def _record_failure(error: BaseException, session_id: str | None = None) -> None:
    if isinstance(error, SystemExit):
        return
    try:
        from capture_diagnostics import record_capture_failure
        from secret_redact import describe_error_chain

        record_capture_failure(
            "antigravity_hook",
            describe_error_chain(error),
            error=error,
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 - a lost trace must not lose the hook's answer
        return


def main() -> int:
    """Run one Antigravity lifecycle callback without risking the host."""
    try:
        _dispatch()
    except (Exception, SystemExit) as error:  # noqa: BLE001
        _record_failure(error)
        print("{}")
        print("antigravity_hook: capture skipped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
