"""When the scheduled maintenance passes run: the one place every backend reads.

The nightly pass runs every evening at 21:00 local time and the weekly pass every
Sunday at 20:00, an hour before that evening's nightly so the two do not overlap.
They moved from 03:00 and Sunday 04:00 because a machine that is switched off at
night never ran them. Task Scheduler, the LaunchAgent, the systemd timer and the
cron fallback are all rendered from these values (`install_control.py`); the
session-start catch-up and the health check ask `due_day` which evening's pass a
moment belongs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class RunTime:
    """A local wall-clock time, and for a weekly pass its day of the week."""

    hour: int
    minute: int = 0
    weekday: int | None = None  # 0 = Sunday, as launchd and cron count

    @property
    def clock(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"


NIGHTLY = RunTime(hour=21)
WEEKLY = RunTime(hour=20, weekday=0)
SCHEDULE = {"nightly": NIGHTLY, "weekly": WEEKLY}

_DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_SYSTEMD_DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def day_name(run: RunTime) -> str:
    """The English day name Task Scheduler takes for a weekly trigger."""
    if run.weekday is None:
        raise ValueError("a nightly run has no day")
    return _DAY_NAMES[run.weekday]


def systemd_calendar(run: RunTime) -> str:
    day = "" if run.weekday is None else f"{_SYSTEMD_DAYS[run.weekday]} "
    return f"{day}*-*-* {run.clock}:00"


def launchd_calendar(run: RunTime) -> dict[str, int]:
    interval = {"Hour": run.hour, "Minute": run.minute}
    if run.weekday is not None:
        interval["Weekday"] = run.weekday
    return interval


def cron_fields(run: RunTime) -> str:
    day = "*" if run.weekday is None else str(run.weekday)
    return f"{run.minute} {run.hour} * * {day}"


def due_day(now: datetime | None = None) -> date:
    """The local day whose nightly pass is the latest one already due at `now`.

    Before the evening's run time the latest due pass is the previous evening's,
    so a morning session does not call last night's run missed.
    """
    moment = now or datetime.now()
    scheduled = moment.replace(hour=NIGHTLY.hour, minute=NIGHTLY.minute, second=0, microsecond=0)
    if moment < scheduled:
        return moment.date() - timedelta(days=1)
    return moment.date()
