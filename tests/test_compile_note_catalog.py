"""The compile sees a catalog of every live note.

Issue #19 of the readable-memory spec, Stage 3. The draft used to receive the
bare list of note paths, and the bodies around it never fitted, so it wrote
blind: it could not tell that a topic already had a note. Both the writer and
the reviewer now read one line per live note — slug, title, one-sentence
summary, type, project and tags — counted in the compile budget, so a covered
topic is updated under its existing slug instead of re-created under a new one.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from llm_client import LLMResult, ProviderDescriptor

DAY = "2026-07-01"
QUOTE = "The backend lease now expires after 45 seconds."
WINDOW_ENV = "MEMORY_COMPILE_CONTEXT_TOKENS"


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
    (daily / f"{DAY}.md").write_text(
        f"# Daily - {DAY}\n\n## [10:00:00] session-end | test\n{QUOTE}\n", encoding="utf-8"
    )
    _note_file(
        notes / "backend-lease.md",
        "type: pattern\ntitle: \"Backend lease\"\nproject: product-a\ntags: [backend, queue]\n",
        "Backend lease",
        "The backend lease expires after a fixed number of seconds.",
    )
    _note_file(
        notes / "alpha-release-owner.md",
        "type: decision\n",
        "Alpha release owner",
        "Every alpha release has exactly one owner.",
    )
    _note_file(
        notes / "old-queue-drain.md",
        "type: pattern\nstatus: superseded\nsuperseded_by: \"[[backend-lease]]\"\n",
        "Old queue drain",
        "The queue drained hourly.",
    )
    (notes / "archive").mkdir()
    _note_file(
        notes / "archive" / "retired-cache.md",
        "type: concept\n",
        "Retired cache",
        "The cache lived in a sibling directory.",
    )
    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
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


def _note_file(path: Path, frontmatter: str, title: str, summary: str) -> None:
    path.write_text(
        f"---\n{frontmatter}---\n\n# {title}\n\nOne-sentence summary: {summary}\n\n"
        "## Lesson\nSettled earlier.\n",
        encoding="utf-8",
    )


def _operation(slug: str, title: str, body: str) -> dict[str, object]:
    return {
        "action": "create",
        "category": "patterns",
        "slug": slug,
        "title": title,
        "summary": f"What {slug} settles.",
        "body_section": "Lesson",
        "body_markdown": body,
        "evidence": [
            {"daily_date": DAY, "timestamp": "10:00:00", "quoted_text": QUOTE, "claim": "Stated."}
        ],
        "related": [],
    }


def _catalog(prompt: str) -> dict[str, dict[str, object]]:
    """The catalog entries a prompt carries, by slug: one JSON object per line."""
    entries = {}
    for line in prompt.splitlines():
        if not line.startswith("{"):
            continue
        entry = json.loads(line)
        entries[entry["slug"]] = entry
    return entries


class FakeModel:
    """Drafts the given operations; the reviewer drops what re-creates a catalog title."""

    def __init__(self, operations: list[dict[str, object]]) -> None:
        self.operations = operations
        self.draft_prompts: list[str] = []
        self.critique_prompts: list[str] = []

    def __call__(self, descriptor, prompt, system_prompt, **kwargs):
        import compile_memory

        if prompt.startswith(compile_memory.CRITIQUE_PROGRAM):
            self.critique_prompts.append(prompt)
            covered = {entry["title"]: slug for slug, entry in _catalog(prompt).items()}
            operations = json.loads(prompt.split("\nOPERATIONS\n", 1)[1].split("\n", 1)[0])
            reviews = [
                {"slug": item["slug"], "verdict": "drop", "reason": f"covered by {covered[item['title']]}"}
                if item["action"] == "create" and item["title"] in covered
                else {"slug": item["slug"], "verdict": "pass", "reason": "ok"}
                for item in operations
            ]
            return LLMResult(descriptor, json.dumps({"reviews": reviews}), True, None, "native")
        self.draft_prompts.append(prompt)
        return LLMResult(
            descriptor, json.dumps({"operations": self.operations, "audit": {}}), True, None, "native"
        )


def _model(monkeypatch: pytest.MonkeyPatch, *operations: dict[str, object]) -> FakeModel:
    import compile_memory

    provider = ProviderDescriptor(
        provider="fake",
        model="fake-model",
        capabilities=MappingProxyType({"structured_output": "native", "max_tokens_enforced": True}),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=0,
        fallback_from=(),
    )
    model = FakeModel(list(operations))
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(compile_memory, "call_candidate", model)
    return model


def _compile() -> int:
    import compile_memory

    return compile_memory._run(argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual"))


def _notes(vault: Path) -> list[str]:
    return sorted(path.name for path in (vault / "knowledge" / "notes").glob("*.md"))


EXPECTED_CATALOG = {
    "alpha-release-owner": {
        "slug": "alpha-release-owner",
        "title": "Alpha release owner",
        "summary": "Every alpha release has exactly one owner.",
        "type": "decision",
    },
    "backend-lease": {
        "slug": "backend-lease",
        "title": "Backend lease",
        "summary": "The backend lease expires after a fixed number of seconds.",
        "type": "pattern",
        "project": "product-a",
        "tags": ["backend", "queue"],
    },
}


def test_the_writer_and_the_reviewer_read_the_catalog_of_live_notes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 0

    [draft] = model.draft_prompts
    [critique] = model.critique_prompts
    for prompt, untrusted in ((draft, "IMMUTABLE SOURCES"), (critique, "OPERATIONS")):
        assert _catalog(prompt) == EXPECTED_CATALOG
        assert prompt.index("EXISTING NOTES") < prompt.index('{"slug":"alpha-release-owner"') < prompt.index(untrusted)
        block = prompt.split("EXISTING NOTES", 1)[1].split("\n\n", 1)[0]
        assert "old-queue-drain" not in block
        assert "retired-cache" not in block
    assert "never create a slug for a topic a catalog entry already covers" in draft.casefold()
    assert "existing slugs are never renamed" in draft.casefold()
    assert "Renew before expiry." in (vault / "knowledge" / "notes" / "backend-renewal.md").read_text(encoding="utf-8")


def test_a_draft_that_reuses_a_catalog_slug_updates_that_note(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _notes(vault)
    _model(monkeypatch, _operation("backend-lease", "Backend lease timing", "The lease now lasts 45 seconds."))

    assert _compile() == 0

    assert _notes(vault) == before
    page = (vault / "knowledge" / "notes" / "backend-lease.md").read_text(encoding="utf-8")
    assert page.startswith("---\ntype: pattern\ntitle: \"Backend lease\"\n")
    assert "\n# Backend lease\n" in page
    assert "Backend lease timing" not in page
    assert "## Update (" in page
    assert page.index("Settled earlier.") < page.index("The lease now lasts 45 seconds.")


def test_the_reviewer_drops_a_new_slug_for_a_covered_topic(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _notes(vault)
    model = _model(
        monkeypatch,
        _operation("backend-lease-expiry", "Backend lease", "The lease now lasts 45 seconds."),
        _operation("backend-renewal", "Backend renewal", "Renew before expiry."),
    )

    assert _compile() == 0

    assert len(model.critique_prompts) == 1
    assert _notes(vault) == sorted([*before, "backend-renewal.md"])


def test_a_long_summary_is_capped_in_the_catalog(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_summary = "The queue keeps one lease per checkout and " + "renews it on every heartbeat " * 40
    _note_file(vault / "knowledge" / "notes" / "queue-lease.md", "type: concept\n", "Queue lease", long_summary)
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 0

    import compile_memory

    [draft] = model.draft_prompts
    summary = str(_catalog(draft)["queue-lease"]["summary"])
    assert summary.startswith("The queue keeps one lease per checkout and renews it")
    assert len(summary) <= compile_memory.CATALOG_SUMMARY_CHARS


def test_a_catalog_the_window_cannot_hold_refuses_the_compile_and_names_the_setting(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for index in range(120):
        _note_file(
            vault / "knowledge" / "notes" / f"backend-topic-{index:03d}.md",
            "type: concept\nproject: product-a\ntags: [backend]\n",
            f"Backend topic {index}",
            f"Backend topic {index} settles one durable rule about the queue and its lease renewal.",
        )
    monkeypatch.setenv(WINDOW_ENV, "8000")
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 1

    out = capsys.readouterr().out
    assert "catalog" in out
    assert WINDOW_ENV in out
    assert model.draft_prompts == []
    assert not (vault / "knowledge" / "notes" / "backend-renewal.md").exists()
