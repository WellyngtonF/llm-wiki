"""User-level SessionEnd hook — tag the day's daily log with the session's project.

Fires at session end from any cwd. Appends a minimal marker entry to
`knowledge/daily/YYYY-MM-DD.md` with the session metadata and, when the
directory belongs to a registered repository, its project and main checkout.
This lets cross-project sessions leave breadcrumbs in the shared daily log.

Companion to the adapter's own session-end capture, which publishes the durable
capture intent the worker classifies. To avoid duplicate work and noisy logs, this
user-level hook **skips** when the current directory is inside the vault — the
vault's own capture already handles that case with richer content.

Contract (hard requirements, mirrors session_start_project_state.py):
    * Must exit 0 on ANY error. Breaking a session-end is worse than a
      missing log entry.
    * Must no-op if LLM_WIKI_ROOT is unset.
    * Reads the SessionEnd payload (session_id, transcript_path, reason)
      from stdin when available — forwards metadata into the daily entry.

Daily entry format (one append per session end):

    ## [HH:MM:SS] session-end | <session_id>
    - Trigger: `<reason>`
    - Agent: `<canonical agent>`
    - Project: `<project>`                      (registered repositories only)
    - Repository: `<main checkout path>`        (registered repositories only)
    - Transcript: `<transcript path>`

This format mirrors the existing project-level entries so downstream
tooling (flush_memory, compile_memory, session_start_context preview)
keeps working without changes.
"""
from __future__ import annotations

import io
import json
import os
import sys
import traceback
from contextlib import suppress
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from daily_log_append import append_deadline, locked_append  # noqa: E402
from event_envelope import canonical_agent  # noqa: E402
from secret_redact import redact_secrets  # noqa: E402


def _resolve_state_root() -> Path | None:
    """Return $LLM_WIKI_STATE_ROOT or the vault root as fallback.

    Mirrors `memory_state.py` convention: if the env var is unset, default
    to the vault itself (runtime dirs cache/logs/run live inside the vault).
    """
    raw = os.environ.get("LLM_WIKI_STATE_ROOT")
    if raw:
        return Path(raw)
    vault = os.environ.get("LLM_WIKI_ROOT")
    if vault:
        return Path(vault).resolve()
    return None


def _safe_write_error(err: str) -> None:
    """Best-effort error log."""
    try:
        state_root = _resolve_state_root()
        if state_root is None:
            return
        log_path = state_root / "logs" / "hook-errors.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] session_end_project_tag: {err}\n")
    except Exception:  # noqa: BLE001
        pass


def _location_lines(project_dir: Path, vault: Path) -> str:
    """`Project:` and `Repository:` for a registered repository; nothing otherwise.

    A session outside every registered repository still leaves its entry, and
    the entry names no project (ADR 0002). A failed lookup is read as unregistered:
    a missing tag is better than a session-end hook that raises.
    """
    try:
        from work_state import placement_of

        placement = placement_of(vault, project_dir)
    except Exception:  # noqa: BLE001
        return ""
    return (
        f"- Project: `{placement.project}`\n"
        f"- Repository: `{placement.repository}`\n"
    )


def _owning_checkout(project_dir: Path, vault: Path) -> Path:
    """The main checkout of the repository the directory belongs to.

    The same rule as `session_start_project_state.repository_of`, imported
    lazily so this thin hook keeps its import cost. A failed import leaves the
    directory as it was: a tag under the directory's own name is better than a
    session-end hook that raises.
    """
    try:
        from session_start_project_state import repository_of
    except Exception:  # noqa: BLE001
        return project_dir
    return repository_of(project_dir, vault / "knowledge" / "projects")


def _resolve_project_dir(vault: Path) -> Path:
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return _owning_checkout(Path(raw).resolve(), vault)


def _read_payload() -> dict:
    """Read the SessionEnd JSON payload from stdin. Return {} on any failure."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def _is_inside_vault(project_dir: Path, vault: Path) -> bool:
    """True if project_dir == vault or is a subdirectory of vault."""
    try:
        project_dir.relative_to(vault)
        return True
    except ValueError:
        return False


def _is_user_home(project_dir: Path) -> bool:
    """True if project_dir is exactly the user's $HOME.

    Same rationale as the HOME guard in session_start_project_state.py:
    $HOME is not a project, and our `.claude/` project marker would
    otherwise match `~/.claude/` user-level config. Prevents `user` slug
    entries in the daily log when Claude Code is launched from $HOME.
    """
    try:
        return project_dir.resolve() == Path.home().resolve()
    except (OSError, RuntimeError):
        return False


def _append_entry(
    daily_path: Path, entry: str, operation_id: str | None = None
) -> None:
    """Append entry to daily log via canonical locked writer, inside the hook's budget."""
    # Read at the call, as the two breadcrumb hooks do: the budget is the shared module's.
    from daily_log_append import LIFECYCLE_APPEND_BUDGET_SECONDS

    locked_append(
        daily_path,
        entry,
        operation_id=operation_id,
        deadline=append_deadline(LIFECYCLE_APPEND_BUDGET_SECONDS),
    )


def _vault_paths() -> tuple[Path, Path] | None:
    vault_root = os.environ.get("LLM_WIKI_ROOT")
    if not vault_root:
        return None
    vault = Path(vault_root).resolve()
    daily_dir = vault / "knowledge" / "daily"
    if daily_dir.parent.is_dir():
        return vault, daily_dir
    _safe_write_error(f"knowledge/ dir missing under {vault}")
    return None


def _eligible_project(vault: Path) -> Path | None:
    project_dir = _resolve_project_dir(vault)
    if _is_inside_vault(project_dir, vault):
        return None
    if _is_user_home(project_dir):
        return None
    return project_dir


def _transcript_line(transcript: str) -> str:
    if not transcript:
        return ""
    return f"- Transcript: `{transcript}`\n"


def _session_entry(payload: dict, location: str, now: datetime) -> str:
    session_id = str(payload.get("session_id", "unknown"))
    reason = str(payload.get("reason", "other"))
    transcript = str(payload.get("transcript_path", ""))
    agent = canonical_agent(str(payload.get("agent") or "claude"))
    entry = (
        f"## [{now.strftime('%H:%M:%S')}] session-end | {session_id}\n"
        f"- Trigger: `{reason}`\n"
        f"- Agent: `{agent}`\n"
        f"{location}"
        f"{_transcript_line(transcript)}\n"
    )
    return redact_secrets(entry)


def _session_operation_id(payload: dict) -> str | None:
    source_event_id = payload.get("event_id") or payload.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id:
        return None
    return f"session-end:{source_event_id}"


def _tag_session() -> bool:
    """True when an entry was appended; False for every skip."""
    paths = _vault_paths()
    if paths is None:
        return False
    vault, daily_dir = paths
    project_dir = _eligible_project(vault)
    if project_dir is None:
        return False
    payload = _read_payload()
    now = datetime.now()
    today_file = daily_dir / f"{now.strftime('%Y-%m-%d')}.md"
    _append_entry(
        today_file,
        _session_entry(payload, _location_lines(project_dir, vault), now),
        operation_id=_session_operation_id(payload),
    )
    return True


def _report(written: bool) -> None:
    """Say on stdout whether a line was written; the exit code stays 0 either way.

    A skip (no vault root, a session inside the vault, a session started in `$HOME`)
    used to be indistinguishable from a write, so `codex_memory daily-log` printed
    "Daily log tagged" for a day nothing was tagged in. See
    `docs/research/2026-09-17-the-six-capture-corrections-the-first-round-left.md`.
    """
    with suppress(OSError, ValueError):
        print(json.dumps({"daily_log_written": written}, ensure_ascii=False))


def main() -> int:
    written = False
    try:
        written = _tag_session()
    except Exception:  # noqa: BLE001
        _safe_write_error("unhandled:\n" + traceback.format_exc())
    _report(written)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
