"""A backlog drains in full batches, not one file change at a time.

A drain cycle rewrites `run/state.json` four times, and a batch ended at the next
checkpoint decision: a file change, every five events or so in a working session. One
Codex session queued 5 964 events on the owner's vault on 2026-09-27; at 15 MB each
rewrite took 1.8 s, the hooks had no time left to drain, and the queue only grew.
Research: `docs/research/2026-09-28-a-backlog-drains-in-full-batches.md`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import integration_adapter  # noqa: E402

START = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)
# What the file-change hook observes, as queued on the owner's vault.
_CHANGED = {"dirty": True, "changed": True, "significant": True}


def _item(index: int) -> dict[str, object]:
    """A working session: four commands, then a file change, over and over."""
    event_id = f"e{index}"
    changed = index % 5 == 4
    kind = "file_changed" if changed else "post_tool_use"
    delta = integration_adapter._empty_delta()
    delta["current_task"] = {"id": "observed", "action": "upsert", "value": f"step {index}"}
    if changed:
        delta["changed_files"] = [{"id": f"file:f{index}", "action": "upsert", "value": f"f{index}"}]
    else:
        delta["commands"] = [{"id": f"command:c{index}", "action": "upsert", "value": f"c{index}"}]
    return {
        "event_id": event_id,
        "state_key": "demo:session",
        "occurred_at": (START + timedelta(seconds=index * 10)).isoformat(),
        "observation": {"type": kind, "event_id": event_id, **(_CHANGED if changed else {})},
        "checkpoint_event": {"trigger": kind, "delta": delta, "evidence_event_ids": [event_id]},
    }


def _plan(count: int):
    items = [_item(index) for index in range(count)]
    return integration_adapter._checkpoint_plan(items, {}, {})


def test_a_short_queue_still_checkpoints_at_its_first_file_change() -> None:
    selected, _reducers, _decisions, decision = _plan(12)

    assert [item["event_id"] for item in selected] == [f"e{index}" for index in range(5)]
    assert decision is not None and decision.reason == "file_change"


def test_a_backlog_takes_the_largest_batch_one_checkpoint_may_carry() -> None:
    window = integration_adapter.PENDING_CLAIM_WINDOW
    selected, _reducers, decisions, decision = _plan(window)

    assert len(selected) == window
    assert [item["event_id"] for item in selected] == [f"e{index}" for index in range(window)]
    assert len(decisions) == window
    assert decision is not None and decision.reason == "batch_flush"
    assert decision.checkpoint_at == datetime.fromisoformat(str(selected[-1]["occurred_at"]))


def test_a_backlog_batch_keeps_every_command_and_file_it_carries() -> None:
    selected, _reducers, _decisions, decision = _plan(integration_adapter.MAX_PENDING_CHECKPOINT_ITEMS)

    merged = integration_adapter._merge_pending_checkpoints(selected, decision)["delta"]

    assert len(merged["changed_files"]) + len(merged["commands"]) == len(selected)
    assert merged["current_task"]["value"] == f"step {len(selected) - 1}"
