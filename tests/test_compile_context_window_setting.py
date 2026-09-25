"""The compile context window is a setting, and each piece of a day is cut to fit it.

Issue #2 of the readable-memory spec, Stage 0. The window was a constant of
32,768 tokens, and a day was cut into 16 KiB pieces whatever the fixed prompt
around them cost, so on a vault with a few hundred notes no piece fitted and
every compile was refused.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from reliable_memory import sha256_bytes

WINDOW_ENV = "MEMORY_COMPILE_CONTEXT_TOKENS"
DAY = "2026-07-01.md"
EMPTY_PLAN = (
    json.dumps(
        {
            "operations": [],
            "audit": {"verified": 0, "dedup": 0, "stubs": 0, "contradictions": 0, "rejected": 0},
        }
    )
    + "\nCOMPILE_AUDIT: verified 0 evidence citations; 0 dedup checks performed; "
    "0 stubs skipped; 0 contradictions handled; 0 pages rejected as below-threshold"
)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import compile_memory
    import memory_state

    vault = tmp_path / "vault"
    daily = vault / "knowledge" / "daily"
    notes = vault / "knowledge" / "notes"
    daily.mkdir(parents=True)
    notes.mkdir(parents=True)
    (vault / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (vault / "knowledge" / "index.md").write_text("# idx\n", encoding="utf-8")
    (vault / "knowledge" / "log.md").write_text("# log\n", encoding="utf-8")
    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.setenv("MEMORY_LLM_FAKE_RESPONSE", EMPTY_PLAN)
    monkeypatch.delenv(WINDOW_ENV, raising=False)
    monkeypatch.setattr(compile_memory, "ROOT", vault)
    monkeypatch.setattr(compile_memory, "STATE_ROOT", state_root)
    monkeypatch.setattr(compile_memory, "MEMORY", vault / "knowledge")
    monkeypatch.setattr(compile_memory, "DAILY_DIR", daily)
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", notes)
    monkeypatch.setattr(compile_memory, "AGENTS", vault / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "INDEX", vault / "knowledge" / "index.md")
    monkeypatch.setattr(compile_memory, "LOG", vault / "knowledge" / "log.md")
    monkeypatch.setattr(memory_state, "ROOT", vault)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_root / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run" / "state.json")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", state_root / "logs")
    return vault


def _one_long_session(vault: Path) -> Path:
    """One captured session of about 40 KB: no entry boundary inside it to cut at."""
    lines = "".join(
        f"- Durable detail {index}: " + "the release keeps one lease per checkout " * 24 + "\n"
        for index in range(40)
    )
    day = vault / "knowledge" / "daily" / DAY
    day.write_text(f"# Daily - 2026-07-01\n\n## [10:00:00] session-end | test\n{lines}", encoding="utf-8")
    return day


def _many_sessions(vault: Path, count: int = 48) -> Path:
    """A long day of many ordinary sessions, about 1 KB each."""
    entries = "".join(
        f"\n## [10:{index % 60:02d}:00] session-end | test\n"
        + "".join(f"- Session {index} fact {line}: the backend keeps one queue.\n" for line in range(18))
        for index in range(count)
    )
    day = vault / "knowledge" / "daily" / DAY
    day.write_text(f"# Daily - 2026-07-01\n{entries}", encoding="utf-8")
    return day


def _existing_notes(vault: Path, count: int = 40) -> None:
    """Enough notes that their list alone costs the prompt about 5 KB."""
    for index in range(count):
        slug = f"note-alpha-{index:03d}-" + "backend-queue-lease-" * 5
        (vault / "knowledge" / "notes" / f"{slug}.md").write_text(
            f"---\ntype: concept\n---\n# Note {index}\n", encoding="utf-8"
        )


def _compile() -> int:
    import compile_memory

    return compile_memory._run(
        argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual")
    )


def _pending() -> list[str]:
    import compile_memory
    from markdown_transaction import active_or_legacy_coordinator

    state = compile_memory.load_state()
    coordinator = active_or_legacy_coordinator(compile_memory.ROOT, compile_memory.STATE_ROOT)
    return [
        path.name
        for path in compile_memory.select_dailies(
            argparse.Namespace(file=None, all=False), state, coordinator=coordinator
        )
    ]


def _mirror(vault: Path) -> dict:
    state = json.loads((vault.parent / "state" / "run" / "state.json").read_text(encoding="utf-8"))
    return state.get("compiled_daily_hashes", {})


def _forget_mirror(vault: Path) -> None:
    """Leave the receipts as the only proof that a day compiled."""
    path = vault.parent / "state" / "run" / "state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("compiled_daily_hashes", None)
    path.write_text(json.dumps(state), encoding="utf-8")


def test_a_day_the_default_window_cannot_take_stays_pending(vault: Path) -> None:
    _one_long_session(vault)

    _compile()

    assert _pending() == [DAY]


def test_the_window_setting_lets_that_day_compile(vault: Path, monkeypatch) -> None:
    day = _one_long_session(vault)
    _compile()
    monkeypatch.setenv(WINDOW_ENV, "272000")

    assert _compile() == 0
    assert _mirror(vault) == {DAY: sha256_bytes(day.read_bytes())}
    assert _pending() == []


def test_a_long_day_compiles_whole_at_a_wide_window(vault: Path, monkeypatch) -> None:
    """Several pieces of one day share a batch once the window has room for them."""
    day = _many_sessions(vault)
    monkeypatch.setenv(WINDOW_ENV, "272000")

    assert _compile() == 0
    assert _mirror(vault) == {DAY: sha256_bytes(day.read_bytes())}
    _forget_mirror(vault)
    assert _pending() == []


def _receipt_sizes(vault: Path) -> list[int]:
    """How many bytes of a day each committed receipt says it covers."""
    sizes = []
    for path in sorted((vault / "knowledge" / "daily" / "receipts").glob("v3-*.md")):
        payload = json.loads(path.read_bytes().split(b"```json", 1)[1].split(b"```", 1)[0])
        sizes.append(payload["source"]["byte_size"])
    return sizes


def test_pieces_are_cut_to_the_room_the_fixed_prompt_leaves(vault: Path, monkeypatch) -> None:
    """16 KiB pieces no longer fit once the note list is counted; smaller ones do.

    Their receipts then prove the day at any window, and once the day grows a
    wider window takes only what no receipt covers yet.
    """
    _existing_notes(vault)
    day = _many_sessions(vault)

    assert _compile() == 0
    assert _mirror(vault) == {DAY: sha256_bytes(day.read_bytes())}
    compiled = _receipt_sizes(vault)
    assert max(compiled) < 16 * 1024
    _forget_mirror(vault)
    assert _pending() == []
    monkeypatch.setenv(WINDOW_ENV, "272000")
    assert _pending() == []

    with day.open("ab") as handle:
        handle.write(b"\n## [23:59:00] session-end | test\n- A late fact about the backend.\n")
    assert _compile() == 0

    grown = _receipt_sizes(vault)
    assert len(grown) == len(compiled) + 1
    assert sum(grown) - sum(compiled) < sum(compiled) // 4
    assert _mirror(vault) == {DAY: sha256_bytes(day.read_bytes())}


def test_evidence_in_a_small_piece_resolves_after_the_day_grows(vault: Path) -> None:
    import compile_memory
    from evidence_resolver import _daily_part_bounds, compile_part_slice

    _existing_notes(vault)
    day = _many_sessions(vault)
    inputs = compile_memory.snapshot_compile_inputs([day])
    batches = compile_memory.pack_compile_batches(inputs, model=None)
    content = day.read_bytes()
    canonical_starts = {start for start, _end in _daily_part_bounds(content)}
    pieces = [piece for batch in batches for piece in batch.inputs.dailies]
    finer = [piece for piece in pieces if piece.byte_start not in canonical_starts]
    grown = content + b"\n## [23:59:00] session-end | test\n- Later.\n"

    assert finer
    assert all(compile_part_slice(grown, piece.sha256) == piece.content for piece in pieces)


@pytest.mark.parametrize("value", ["lots", "4096", "-1"])
def test_an_unusable_window_refuses_the_compile_and_says_why(vault: Path, monkeypatch, capsys, value) -> None:
    _one_long_session(vault)
    monkeypatch.setenv(WINDOW_ENV, value)

    assert _compile() == 1
    assert WINDOW_ENV in capsys.readouterr().out
    assert _pending() == [DAY]
