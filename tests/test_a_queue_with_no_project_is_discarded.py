"""A checkpoint queue whose directory belongs to no registered project is discarded.

Unregistered work writes no work state (ADR 0002), but queues enqueued before that rule
stayed in `run/state.json`: every nightly drain refused them as `Unregistered` and kept
them, six of them on the owner's vault four days after the migration that should have
cleared them. Research:
`docs/research/2026-09-28-a-queue-with-no-project-is-discarded.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_adapter  # noqa: E402
from work_state import Unregistered  # noqa: E402

NEWEST = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path, monkeypatch):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(integration_adapter, "update_state", memory_state.update_state)
    return memory_state


def _event(event_id: str, occurred_at: datetime) -> dict[str, object]:
    return {"event_id": event_id, "state_key": f"{event_id}:s", "occurred_at": occurred_at.isoformat()}


def _unregistered(slug, queue_key, **kwargs):
    raise Unregistered(f"work state {slug!r} belongs to no registered repository")


def test_an_old_queue_with_no_project_is_discarded(state, monkeypatch):
    state.save_state({
        "project_checkpoint_pending": {
            "sandbox-probe": [_event("old", NEWEST - timedelta(days=4))],
            "registered": [_event("new", NEWEST)],
        },
        "project_checkpoint_inflight": {"sandbox-probe": {"event_ids": ["old"]}},
    })
    monkeypatch.setattr(
        integration_adapter,
        "_drain_project_checkpoints",
        lambda slug, queue_key, **kwargs: _unregistered(slug, queue_key) if slug == "sandbox-probe" else None,
    )

    result = integration_adapter.drain_pending_backlog(5.0)

    saved = state.load_state()
    assert "sandbox-probe" not in saved["project_checkpoint_pending"]
    assert "sandbox-probe" not in saved["project_checkpoint_inflight"]
    assert "registered" in saved["project_checkpoint_pending"]
    assert "sandbox-probe" not in result["failed"]
    assert result["drained"]["sandbox-probe"] == 1
    assert result["discarded"] == {"sandbox-probe": 1}


def test_a_recent_queue_is_kept_in_case_its_repository_is_only_briefly_unreadable(
    state, monkeypatch
):
    state.save_state({
        "project_checkpoint_pending": {"briefly-lost": [_event("recent", NEWEST - timedelta(hours=2))]},
    })
    monkeypatch.setattr(integration_adapter, "_drain_project_checkpoints", _unregistered)

    result = integration_adapter.drain_pending_backlog(5.0)

    assert "briefly-lost" in state.load_state()["project_checkpoint_pending"]
    assert result["failed"]["briefly-lost"].startswith("Unregistered")
    assert result["discarded"] == {}
