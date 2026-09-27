"""Another process's short write does not fail a compile that took minutes to plan.

The compile refused every model call while *any* process held the global writer gate
for 10 s, and gave its publication the same 10 s. With three agent sessions and the
nightly writing, one compile on the owner's vault planned five batches for sixteen
minutes and then died on the gate, and the nightly behind it failed three evenings in
a row. Research:
`docs/research/2026-09-27-a-busy-gate-does-not-fail-the-compile.md`.
"""
from __future__ import annotations

import contextlib
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import SHORT_TIMEOUT


@pytest.fixture
def coordinators(tmp_path: Path) -> tuple[MarkdownCoordinator, MarkdownCoordinator]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    (root / "knowledge/notes").mkdir(parents=True)
    return MarkdownCoordinator(root, state_root), MarkdownCoordinator(root, state_root)


def test_a_model_call_does_not_wait_for_another_writer(coordinators):
    import compile_memory

    owner, observer = coordinators
    acquired = threading.Event()
    release = threading.Event()

    def write():
        with owner.writer_gate():
            acquired.set()
            release.wait(SHORT_TIMEOUT)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(write)
        assert acquired.wait(SHORT_TIMEOUT)
        try:
            compile_memory._assert_external_work_allowed(observer)
            assert not release.is_set(), "the other writer still held the gate"
        finally:
            release.set()
        future.result(timeout=SHORT_TIMEOUT)


def test_a_model_call_is_still_refused_under_this_threads_own_gate(coordinators):
    import compile_memory

    owner, observer = coordinators
    with owner.writer_gate():
        with pytest.raises(RuntimeError, match="writer"):
            compile_memory._assert_external_work_allowed(observer)


class _Coordinator:
    def __init__(self) -> None:
        self.waits: list[float | None] = []

    @contextlib.contextmanager
    def writer_gate(self, *, owner=None, wait_seconds=None):
        self.waits.append(wait_seconds)
        yield owner

    def recover(self, **_kwargs) -> None:
        return None


class _Publication:
    def assess_claims(self) -> None:
        return None

    def publish(self) -> str:
        return "published"


def test_publication_waits_out_a_writer_longer_than_a_hook_would(monkeypatch):
    import compile_memory
    import markdown_transaction

    coordinator = _Coordinator()
    result = compile_memory._published_once(
        _Publication(), coordinator, None, math.inf, None
    )

    assert result == "published"
    assert coordinator.waits == [compile_memory.COMPILE_PUBLICATION_GATE_SECONDS]
    assert compile_memory.COMPILE_PUBLICATION_GATE_SECONDS > markdown_transaction._WRITER_WAIT_SECONDS


def test_publication_never_waits_past_the_compile_deadline():
    import compile_memory

    coordinator = _Coordinator()
    compile_memory._published_once(
        _Publication(), coordinator, None, time.monotonic() + 5.0, None
    )
    compile_memory._published_once(
        _Publication(), coordinator, None, time.monotonic() - 1.0, None
    )

    assert 0 < coordinator.waits[0] <= 5.0
    assert coordinator.waits[1] == 0.0
