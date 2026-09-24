"""A capture append whose fence lapsed stops at once instead of spending its ids.

On 2026-09-23 one capture's fence expired half a second into its append. The
lapsed fence failed every attempt's precondition the way a lost compare-and-swap
does, so all 64 deterministic candidate ids were quarantined; each later retry of
the task found them spent and died in four seconds, and the capture was lost.
Doctor, meanwhile, called a refusal that arrived before the plan existed "a state
this runtime does not define".
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import doctor
import markdown_transaction
import pytest
from markdown_transaction import MarkdownCoordinator, TransactionFailure

_LOG = "knowledge/daily/2026-09-23.md"


def _coordinator(tmp_path: Path) -> MarkdownCoordinator:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    return MarkdownCoordinator(root, tmp_path / "state")


def _states(coordinator: MarkdownCoordinator) -> list[str]:
    with sqlite3.connect(coordinator.database_path) as database:
        return [row[0] for row in database.execute('SELECT state FROM "transaction"')]


def _fence_lost() -> None:
    raise RuntimeError("intent_fence_lost")


def test_a_lost_fence_stops_the_loop_before_any_attempt_is_spent(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)

    with pytest.raises(RuntimeError, match="intent_fence_lost"):
        markdown_transaction._append_until_committed(
            coordinator,
            "capture-markdown:intent",
            _LOG,
            b"line\n",
            deadline=float("inf"),
            cancelled=None,
            require_live=_fence_lost,
        )

    assert _states(coordinator) == []


def test_a_refused_fence_check_is_named_as_a_lost_fence(tmp_path: Path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)

    def refused(_database, _expected) -> None:
        raise TransactionFailure("persisted capture precondition failed", "precondition_failed", "quarantined")

    monkeypatch.setattr(coordinator, "_check_capture_preconditions", refused)

    with pytest.raises(RuntimeError, match="intent_fence_lost"):
        markdown_transaction._require_capture_still_fenced(coordinator, {})


def test_the_captured_append_checks_its_fence_on_every_attempt(tmp_path: Path, monkeypatch) -> None:
    coordinator = _coordinator(tmp_path)
    checks: list[int] = []

    def check(_database, _expected) -> None:
        checks.append(len(checks))
        if len(checks) > 1:
            raise TransactionFailure("persisted capture precondition failed", "precondition_failed", "quarantined")

    def loop(*_args, require_live, **_kwargs):
        require_live()

    monkeypatch.setattr(coordinator, "_validate_preconditions", lambda preconditions: preconditions)
    monkeypatch.setattr(coordinator, "_check_capture_preconditions", check)
    monkeypatch.setattr(coordinator, "writer_gate", lambda owner: contextlib.nullcontext())
    monkeypatch.setattr(markdown_transaction, "_recover_initial_contention", lambda *_a, **_k: None)
    monkeypatch.setattr(markdown_transaction, "_append_until_committed", loop)

    with pytest.raises(RuntimeError, match="intent_fence_lost"):
        markdown_transaction.append_captured_knowledge(
            coordinator, object(), "capture-markdown:intent", tmp_path / "vault" / _LOG, b"line\n", preconditions={}
        )

    assert checks == [0, 1]


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "c27b8da1eb114c5faf225e999750394b",
        "operation_id": "project:verum-oms-backend:336:attempt:1:epoch:2992:digest",
        "request_hash": "7" * 64,
        "state": "quarantined",
        "preconditions_json": "{}",
        "plan_hash": "",
        "created_at": "2026-09-22T22:54:37.234340Z",
        "updated_at": "2026-09-22T22:55:07.214840Z",
        "artifacts_pruned_at": None,
    }
    row.update(overrides)
    return row


def test_a_refusal_before_the_plan_existed_is_not_corrupt() -> None:
    row = _row()

    assert doctor._transaction_row_corrupt(row, "quarantined", {row["id"]: []}) is False


def test_an_unplanned_quarantine_that_owns_operations_is_still_corrupt() -> None:
    row = _row()

    assert doctor._transaction_row_corrupt(row, "quarantined", {row["id"]: [0]}) is True


def test_an_unplanned_quarantine_with_a_broken_identity_is_still_corrupt() -> None:
    row = _row(request_hash="not-a-digest")

    assert doctor._transaction_row_corrupt(row, "quarantined", {row["id"]: []}) is True
