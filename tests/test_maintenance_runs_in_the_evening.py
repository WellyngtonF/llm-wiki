"""The maintenance passes run in the evening, and every backend says so from one place.

A machine switched off at night never ran the 03:00 nightly or the Sunday 04:00
weekly. They now run at 21:00 and on Sunday at 20:00, an hour before that
evening's nightly, and the scheduler backends, the session-start catch-up and the
nightly's session consolidation all read `maintenance_schedule`.
"""
from __future__ import annotations

import json
import plistlib
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import episode_consolidation  # noqa: E402
import install_control  # noqa: E402
import maintenance_schedule  # noqa: E402
import scheduled_nightly  # noqa: E402
import session_start_context  # noqa: E402


def test_the_nightly_runs_at_nine_and_the_weekly_an_hour_before_it_on_sunday() -> None:
    nightly, weekly = maintenance_schedule.NIGHTLY, maintenance_schedule.WEEKLY

    assert (nightly.clock, weekly.clock, maintenance_schedule.day_name(weekly)) == (
        "21:00",
        "20:00",
        "Sunday",
    )
    assert weekly.hour + 1 == nightly.hour


def test_every_scheduler_backend_renders_the_same_times(tmp_path: Path) -> None:
    root, state, uv_path = tmp_path / "vault", tmp_path / "state", tmp_path / "bin" / "uv"

    systemd = install_control.render_systemd_definitions(root, state, uv_path)
    launchd = install_control.render_launchd_definitions(root, state, uv_path)
    cron = install_control.render_cron_block(root, state, uv_path).decode().splitlines()
    windows = json.loads(install_control.render_windows_task_spec(root, state, uv_path))

    assert "OnCalendar=*-*-* 21:00:00" in systemd["llm-wiki-nightly.timer"].decode()
    assert "OnCalendar=Sun *-*-* 20:00:00" in systemd["llm-wiki-weekly.timer"].decode()
    assert [
        plistlib.loads(launchd[f"io.github.ekgardt.llm-wiki.{kind}.plist"])["StartCalendarInterval"]
        for kind in ("nightly", "weekly")
    ] == [{"Hour": 21, "Minute": 0}, {"Hour": 20, "Minute": 0, "Weekday": 0}]
    assert [line.split(" ", 5)[:5] for line in cron[1:3]] == [
        ["0", "21", "*", "*", "*"],
        ["0", "20", "*", "*", "0"],
    ]
    assert [(task["at"], task.get("day")) for task in windows["tasks"]] == [
        ("21:00", None),
        ("20:00", "Sunday"),
    ]


def test_the_windows_command_carries_the_times_of_its_specification(tmp_path: Path) -> None:
    spec = json.loads(
        install_control.render_windows_task_spec(tmp_path / "v", tmp_path / "s", tmp_path / "uv.exe")
    )

    command = install_control._windows_task_command_from_spec(
        spec, powershell="pwsh.exe", script_path=tmp_path / "t.ps1", mode=None
    )

    def value(flag: str) -> str:
        return command[command.index(flag) + 1]

    assert (value("-SpecVersion"), value("-NightlyAt"), value("-WeeklyAt"), value("-WeeklyDay")) == (
        str(install_control.WINDOWS_TASK_SPEC_VERSION),
        "21:00",
        "20:00",
        "Sunday",
    )


def test_a_morning_session_does_not_call_last_evenings_nightly_missed(tmp_path, monkeypatch) -> None:
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    memory_state.save_state({"last_nightly_date": "2026-09-24"})
    morning = datetime(2026, 9, 25, 8, 30)
    evening = datetime(2026, 9, 25, 21, 30)

    assert (
        str(maintenance_schedule.due_day(morning)),
        str(maintenance_schedule.due_day(evening)),
    ) == ("2026-09-24", "2026-09-25")
    monkeypatch.setattr(maintenance_schedule, "due_day", lambda now=None: morning.date().replace(day=24))
    assert session_start_context._claim_nightly_catchup() is False
    monkeypatch.setattr(maintenance_schedule, "due_day", lambda now=None: evening.date())
    assert session_start_context._claim_nightly_catchup() is True


def test_a_catch_up_that_ran_the_next_morning_is_not_asked_for_again(tmp_path, monkeypatch) -> None:
    """It records its own date, which is after the evening it stood in for."""
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    memory_state.save_state({"last_nightly_date": "2026-09-25"})

    assert session_start_context._claim_nightly_catchup("2026-09-24") is False


def _records(vault: Path, day: str) -> None:
    directory = vault / "knowledge/raw/sessions" / day
    directory.mkdir(parents=True)
    (directory / "s.md").write_text("# S\n\nuser: line\n", encoding="utf-8")


def test_the_evening_pass_consolidates_todays_sessions(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _records(vault, "2026-09-24")
    _records(vault, "2026-09-25")

    assert (
        episode_consolidation.pending_days(vault, {}, today="2026-09-25"),
        episode_consolidation.pending_days(vault, {}, today="2026-09-25", include_today=True),
    ) == (["2026-09-24"], ["2026-09-24", "2026-09-25"])


def test_the_nightly_asks_consolidation_for_today_too() -> None:
    assert "--include-today" in scheduled_nightly._episode_step().command


def test_the_consolidation_command_line_includes_today_when_asked(tmp_path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    _records(vault, datetime.now().strftime("%Y-%m-%d"))
    monkeypatch.setattr(episode_consolidation, "_safe_state", lambda: {})

    args = episode_consolidation.parse_args(["--vault", str(vault), "--all-pending", "--include-today"])

    assert episode_consolidation._selected_days(args) == [datetime.now().strftime("%Y-%m-%d")]
