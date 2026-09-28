"""A nightly that ran and failed is not started again by every session that opens.

Only a success recorded a date, so after a failed pass each session start saw the due
evening as missed and started the whole nightly again as soon as the 30-minute claim
lapsed: four passes one evening on the owner's machine, and one more at every login
while the last one was still failing. Research:
`docs/research/2026-09-28-a-failed-nightly-is-not-rerun-by-every-session.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import session_start_context  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    import memory_state

    monkeypatch.setattr(memory_state, "STATE_DIR", tmp_path / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", tmp_path / "run" / "state.json")
    monkeypatch.setattr(memory_state, "LOCK_FILE", tmp_path / "run" / "state.json.lock")
    monkeypatch.setattr(session_start_context, "update_state", memory_state.update_state)
    return memory_state


def test_a_pass_that_failed_for_the_due_evening_is_not_caught_up(state):
    state.save_state({
        "last_nightly_date": "2026-09-25",
        "last_nightly_failure": {"date": "2026-09-26", "failures": 1},
    })

    assert session_start_context._claim_nightly_catchup("2026-09-26") is False


def test_a_catch_up_that_failed_the_next_morning_is_not_caught_up_again(state):
    state.save_state({
        "last_nightly_date": "2026-09-25",
        "last_nightly_failure": {"date": "2026-09-27", "failures": 1},
    })

    assert session_start_context._claim_nightly_catchup("2026-09-26") is False


def test_a_failure_older_than_the_due_evening_is_still_caught_up(state):
    state.save_state({
        "last_nightly_date": "2026-09-24",
        "last_nightly_failure": {"date": "2026-09-25", "failures": 1},
    })

    assert session_start_context._claim_nightly_catchup("2026-09-26") is True
