"""The compile keeps only links to notes that exist.

Issue #9 of the readable-memory spec, Stage 1. The model proposes `related`
links on create and on update. They are checked against the live notes of the
snapshot plus the notes created in the same batch; an unknown link is dropped
and named in the vault log, and an update now adds its valid links to the
page's `## Related` section.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from types import MappingProxyType

import pytest
from llm_client import LLMResult, ProviderDescriptor

FIRST_DAY = "2026-07-01"
SECOND_DAY = "2026-07-02"
FIRST_QUOTE = "The backend lease expires after 30 seconds."
SECOND_QUOTE = "The backend lease is refreshed every 10 seconds."


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import compile_memory
    import memory_state

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    daily = vault / "knowledge" / "daily"
    daily.mkdir(parents=True)
    notes.mkdir(parents=True)
    (vault / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (vault / "knowledge" / "index.md").write_text("# idx\n", encoding="utf-8")
    (vault / "knowledge" / "log.md").write_text("# Session Memory Log\n", encoding="utf-8")
    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
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


def _note(vault: Path, slug: str, *, status: str | None = None) -> None:
    status_line = f"status: {status}\n" if status else ""
    (vault / "knowledge" / "notes" / f"{slug}.md").write_text(
        f"---\ntype: concept\n{status_line}---\n# {slug}\n\nOne-sentence summary: {slug}.\n",
        encoding="utf-8",
    )


def _day(vault: Path, date: str, quote: str) -> None:
    (vault / "knowledge" / "daily" / f"{date}.md").write_text(
        f"# Daily - {date}\n\n## [10:00:00] session-end | test\n{quote}\n", encoding="utf-8"
    )


def _operation(
    slug: str, date: str, quote: str, related: list[str], claims: list[object] | None = None
) -> dict[str, object]:
    operation: dict[str, object] = {
        "action": "create",
        "category": "patterns",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "summary": f"What {slug} settles.",
        "body_section": "Lesson",
        "body_markdown": f"The lesson of {slug}.",
        "evidence": [
            {"daily_date": date, "timestamp": "10:00:00", "quoted_text": quote, "claim": "Stated."}
        ],
        "related": related,
    }
    if claims is not None:
        operation["claims"] = claims
    return operation


def _provider() -> ProviderDescriptor:
    return ProviderDescriptor(
        provider="fake",
        model="fake-model",
        capabilities=MappingProxyType({"structured_output": "native", "max_tokens_enforced": True}),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=0,
        fallback_from=(),
    )


def _drafts(monkeypatch: pytest.MonkeyPatch, operations: list[dict[str, object]]) -> list[str]:
    """The model drafts these operations and the critique passes every one of them."""
    import compile_memory

    prompts: list[str] = []
    provider = _provider()
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)

    def call(descriptor, prompt, system_prompt, **kwargs):
        prompts.append(prompt)
        if prompt.startswith(compile_memory.CRITIQUE_PROGRAM):
            reviews = [{"slug": item["slug"], "verdict": "pass", "reason": "ok"} for item in operations]
            return LLMResult(descriptor, json.dumps({"reviews": reviews}), True, None, "native")
        return LLMResult(descriptor, json.dumps({"operations": operations, "audit": {}}), True, None, "native")

    monkeypatch.setattr(compile_memory, "call_candidate", call)
    return prompts


def _compile() -> int:
    import compile_memory

    return compile_memory._run(argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual"))


def _page(vault: Path, slug: str) -> str:
    return (vault / "knowledge" / "notes" / f"{slug}.md").read_text(encoding="utf-8")


def _related(page: str) -> list[str]:
    [section] = re.findall(r"(?ms)^## Related[ \t]*\n(.*?)(?=^#{1,2} |\Z)", page)
    return re.findall(r"^- (\S.*?)\s*$", section, re.MULTILINE)


def _log(vault: Path) -> str:
    return (vault / "knowledge" / "log.md").read_text(encoding="utf-8")


def _first_compile(vault: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _note(vault, "alpha-queue")
    _note(vault, "old-queue", status="superseded")
    _day(vault, FIRST_DAY, FIRST_QUOTE)
    prompts = _drafts(
        monkeypatch,
        [
            _operation(
                "backend-lease",
                FIRST_DAY,
                FIRST_QUOTE,
                [
                    "[[alpha-queue]]",
                    "[[release-owner]]",
                    "[[missing-note]]",
                    "[[backend-lease]]",
                    "[[knowledge/notes/alpha-queue]]",
                    "[[old-queue]]",
                ],
                claims=[
                    {
                        "evidence_index": 0,
                        "subject": "backend lease",
                        "relation": "ends-at",
                        "value": {"type": "number", "value": "30", "unit": "seconds"},
                    }
                ],
            ),
            _operation("release-owner", FIRST_DAY, FIRST_QUOTE, ["[[notes/backend-lease|the lease]]"]),
        ],
    )
    assert _compile() == 0
    return prompts


def test_a_created_note_links_only_to_notes_that_exist_or_are_created_with_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts = _first_compile(vault, monkeypatch)

    assert _related(_page(vault, "backend-lease")) == ["[[alpha-queue]]", "[[release-owner]]"]
    assert _related(_page(vault, "release-owner")) == ["[[backend-lease|the lease]]"]
    [entry] = [line for line in _log(vault).splitlines() if "compile completed" in line]
    assert "Dropped links: [[missing-note]], [[old-queue]] (from backend-lease)." in entry
    draft = next(prompt for prompt in prompts if prompt.startswith("compile-draft/"))
    assert "bare [[slug]]" in draft
    assert "created in this same plan" in draft


def test_an_update_adds_its_new_valid_links_to_related_and_keeps_the_ledger(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claims import parse_claim_ledger

    _first_compile(vault, monkeypatch)
    _note(vault, "gamma-note")
    _day(vault, SECOND_DAY, SECOND_QUOTE)
    _drafts(
        monkeypatch,
        [
            _operation(
                "backend-lease",
                SECOND_DAY,
                SECOND_QUOTE,
                ["[[release-owner]]", "[[gamma-note]]", "[[nowhere]]", "[[gamma-note]]"],
            )
        ],
    )

    assert _compile() == 0

    page = _page(vault, "backend-lease")
    assert _related(page) == ["[[alpha-queue]]", "[[release-owner]]", "[[gamma-note]]"]
    assert page.index("## Related") < page.index("## Claims")
    ledger = parse_claim_ledger(page.encode("utf-8"))
    assert ledger is not None
    assert [claim["subject"] for claim in ledger["claims"]] == ["backend lease"]
    entries = [line for line in _log(vault).splitlines() if "compile completed" in line]
    assert len(entries) == 2
    assert entries[1].endswith("Dropped links: [[nowhere]] (from backend-lease).")


def test_an_update_of_a_page_without_related_opens_the_section_before_the_ledger(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claims import parse_claim_ledger

    _first_compile(vault, monkeypatch)
    page_path = vault / "knowledge" / "notes" / "backend-lease.md"
    page_path.write_text(
        _page(vault, "backend-lease").replace(
            "\n## Related\n- [[alpha-queue]]\n- [[release-owner]]\n", "\n"
        ),
        encoding="utf-8",
    )
    _day(vault, SECOND_DAY, SECOND_QUOTE)
    _drafts(monkeypatch, [_operation("backend-lease", SECOND_DAY, SECOND_QUOTE, ["[[alpha-queue]]"])])

    assert _compile() == 0

    page = _page(vault, "backend-lease")
    assert _related(page) == ["[[alpha-queue]]"]
    assert page.index("## Related") < page.index("## Claims")
    ledger = parse_claim_ledger(page.encode("utf-8"))
    assert ledger is not None
    assert [claim["subject"] for claim in ledger["claims"]] == ["backend lease"]
    [_first, second] = [line for line in _log(vault).splitlines() if "compile completed" in line]
    assert "Dropped links" not in second
