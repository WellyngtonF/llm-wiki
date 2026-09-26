"""A hook never starts a project checkpoint write its host would cancel halfway.

A hook waited up to 10 s for the global writer gate inside a host timeout of 5 s. The
one that got the gate late was killed mid-write, the gate stayed held until its 30 s
lease lapsed, and every hook behind it waited, was killed and held it again: fifteen
wedged checkpoints on the owner's vault in one afternoon, and the compile refused
behind them. Research:
`docs/research/2026-09-26-a-hook-never-starts-a-write-it-cannot-finish.md`.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_adapter  # noqa: E402

# These tests read the shipped budgets; every other test gets the slow-machine bound.
pytestmark = pytest.mark.shipped_append_budgets

SHIPPED = (
    ROOT / "integrations/claude-code/settings.json",
    ROOT / "integrations/codex/hooks.json",
)
CODEX_HOOK_EVENTS = {"SessionStart": "session_start", "PreCompact": "pre_compact", "Stop": "stop"}


def _shipped_timeouts() -> dict[str, list[float]]:
    """Every shipped timeout of a hook that reaches `ingest_event`, by envelope event."""
    found: dict[str, list[float]] = {}
    for path in SHIPPED:
        hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        for host_event, groups in hooks.items():
            for handler in (h for group in groups for h in group["hooks"]):
                command = handler["command"]
                named = re.search(r"integration_adapter\.py.*--event (\w+)", command)
                event = named[1] if named else None
                if event is None and "codex_memory.py" in command:
                    event = CODEX_HOOK_EVENTS.get(host_event)
                if event is not None:
                    found.setdefault(event, []).append(float(handler["timeout"]))
    return found


def test_every_hook_budget_fits_inside_its_shipped_host_timeout():
    shipped = _shipped_timeouts()

    assert set(shipped) <= set(integration_adapter.HOOK_HOST_SECONDS)
    assert {
        event: integration_adapter.HOOK_HOST_SECONDS[event] <= min(timeouts)
        for event, timeouts in shipped.items()
    } == {event: True for event in shipped}


def test_the_shortest_hook_still_has_time_to_write_one_checkpoint():
    shortest = min(integration_adapter.HOOK_HOST_SECONDS.values())

    assert shortest - integration_adapter.HOOK_EXIT_ROOM_SECONDS > (
        integration_adapter.CHECKPOINT_WRITE_SECONDS
    )


class _Store:
    def __init__(self, calls: list[object]):
        self.calls = calls

    def checkpoint(self, slug, event, owner, *, writer_wait_seconds=None):
        self.calls.append(("checkpoint", writer_wait_seconds))


@pytest.fixture
def hook(monkeypatch):
    from work_state import Placement

    state: dict = {}
    calls: list[object] = []

    def update(mutator, **kwargs):
        mutator(state)
        return state

    monkeypatch.setattr(integration_adapter, "update_state", update)
    monkeypatch.setattr(
        integration_adapter,
        "work_state_store",
        lambda *args, **kwargs: (_Store(calls), "demo"),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_project_context",
        lambda event: (Placement("demo", ROOT, "demo"), ROOT),
    )
    monkeypatch.setattr(
        integration_adapter,
        "_ingest_session_end",
        lambda *args: calls.append("capture"),
    )
    delta = {"current_task": {"id": "task-1", "action": "upsert", "value": "Ship login"}}
    envelope = integration_adapter.normalize_event(
        "codex",
        "session_end",
        {"session_id": "s1", "cwd": "C:/p", "event_id": "one", "project_delta": delta},
    )
    return envelope, state, calls


def test_a_hook_runs_its_capture_before_the_checkpoint_write(hook):
    envelope, state, calls = hook

    integration_adapter.ingest_event(envelope, deadline=time.monotonic() + 60)

    assert [call if call == "capture" else call[0] for call in calls] == [
        "capture",
        "checkpoint",
    ]
    assert state["project_checkpoint_pending"]["demo"] == []


def test_a_write_is_given_no_more_than_the_time_before_its_last_start(hook):
    envelope, _state, calls = hook
    deadline = time.monotonic() + integration_adapter.CHECKPOINT_WRITE_SECONDS + 1.0

    integration_adapter.ingest_event(envelope, deadline=deadline)

    (_name, wait), = [call for call in calls if call != "capture"]
    assert 0 < wait <= 1.0


def test_a_hook_past_its_last_start_leaves_the_event_pending_and_writes_nothing(hook):
    envelope, state, calls = hook
    deadline = time.monotonic() + integration_adapter.CHECKPOINT_WRITE_SECONDS - 0.1

    integration_adapter.ingest_event(envelope, deadline=deadline)

    assert calls == ["capture"]
    assert [item["event_id"] for item in state["project_checkpoint_pending"]["demo"]] == [
        envelope.event_id
    ]
    assert not any(item.get("claim_owner") for item in state["project_checkpoint_pending"]["demo"])


def test_a_library_caller_keeps_the_checkpoint_first_and_unbounded(hook):
    envelope, _state, calls = hook

    integration_adapter.ingest_event(envelope)

    assert calls == [("checkpoint", None), "capture"]
