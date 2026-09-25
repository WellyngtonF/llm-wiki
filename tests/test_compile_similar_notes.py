"""The compile reads the most similar notes in full.

Issue #21 of the readable-memory spec, Stage 3. The catalog (#19) tells the
writer that a topic is covered, not what the note already says. For each
daily-log piece the compile now retrieves the live notes most similar to it
from the active evidence generation and gives their text, bounded by the
compile window, to the writer and to the reviewer. The reviewer may name the
existing note an operation duplicates: the operation becomes an update of that
note, or is dropped when the named note is not live. Without vectors the
compile reads the catalog alone and says so once.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from llm_client import LLMResult, ProviderDescriptor

np = pytest.importorskip("numpy")

DAY = "2026-07-01"
QUOTE = "The backend lease now expires after 45 seconds and the heartbeat renews it."
LEASE_BODY = "A heartbeat renews the backend lease before it expires; a missed renewal frees it."
ALPHA_BODY = "Releases are signed off by their owner on the release branch."
LEDGER_TEXT = "claim-2026-06-01-0000"
EVIDENCE_TEXT = "daily:2026-06-01 sha256:" + "a" * 64
VOCABULARY = ("lease", "heartbeat", "backend", "expires", "release", "owner", "signed")
UNAVAILABLE = "similar notes unavailable"


def _embed(texts: list[str]) -> object:
    """A bag of words over a tiny vocabulary: text about leases lands near leases."""
    rows = []
    for text in texts:
        lowered = str(text).casefold()
        row = np.array([lowered.count(word) for word in VOCABULARY], dtype=np.float32)
        norm = float(np.linalg.norm(row))
        rows.append(row / norm if norm else np.full(len(VOCABULARY), 1e-3, dtype=np.float32))
    return np.vstack(rows)


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
    _note_file(notes / "backend-lease.md", "type: pattern\n", "Backend lease", "How the lease is held.", LEASE_BODY)
    _note_file(notes / "alpha-release-owner.md", "type: decision\n", "Alpha release owner", "Who owns a release.", ALPHA_BODY)
    _note_file(
        notes / "old-queue-drain.md",
        "type: pattern\nstatus: superseded\nsuperseded_by: \"[[backend-lease]]\"\n",
        "Old queue drain",
        "The queue drained hourly.",
        "The drain ran every hour.",
    )
    state_root = tmp_path / "state"
    (state_root / "run").mkdir(parents=True)
    (state_root / "logs").mkdir(parents=True)

    monkeypatch.setenv("LLM_WIKI_ROOT", str(vault))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    monkeypatch.setenv("MEMORY_LLM_PROVIDER", "fake")
    monkeypatch.delenv("MEMORY_COMPILE_CONTEXT_TOKENS", raising=False)
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


def _note_file(path: Path, frontmatter: str, title: str, summary: str, body: str) -> None:
    path.write_text(
        f"---\n{frontmatter}---\n\n# {title}\n\nOne-sentence summary: {summary}\n\n"
        f"## Lesson\n{body}\n\n"
        f"## Evidence\n- `{EVIDENCE_TEXT} block:09:00:00 bytes:0-10` — Stated.\n\n"
        "## Claims\n```json\n"
        f'{{"claims":[{{"id":"{LEDGER_TEXT}"}}],"schema_version":"claim-ledger/v1"}}\n'
        "```\n",
        encoding="utf-8",
    )


def _search_over(vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, vector_state: str) -> None:
    """An active evidence generation of this vault, its vectors made by `_embed`."""
    import search_memory
    from corpus_snapshot import collect_corpus
    from repository_scope import resolve_repository_scope

    notes = vault / "knowledge" / "notes"
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    snapshot = collect_corpus(vault)
    catalog = search_memory.GenerationCatalog(tmp_path / "search-state")
    generation = catalog.generations_path / "gen-similar"
    generation.mkdir(parents=True)
    artifacts = [search_memory.build_generation_fts(snapshot, generation)]
    artifacts.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=_embed,
            model_id=search_memory.EMBEDDING_MODEL,
            model_revision=search_memory.EMBEDDING_MODEL_REVISION,
            dimensions=len(VOCABULARY),
        )
    )
    manifest = {
        "generation_id": "gen-similar",
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": search_memory.EMBEDDING_MODEL,
        "embedding_model_revision": search_memory.EMBEDDING_MODEL_REVISION,
        "vector_dimensions": len(VOCABULARY),
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": artifacts,
        "vector_state": vector_state,
        "repository_scope": resolve_repository_scope(vault).as_dict(),
    }

    class Catalog:
        generations_path = catalog.generations_path

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return manifest

    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: Catalog())
    monkeypatch.setattr(search_memory, "_lazy_generation_query_encoder", lambda: _embed)


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


class FakeModel:
    """Drafts the given operations; reviews each with the verdict given for its slug."""

    def __init__(self, operations: list[dict[str, object]], reviews: dict[str, dict[str, str]]) -> None:
        self.operations = operations
        self.reviews = reviews
        self.draft_prompts: list[str] = []
        self.critique_prompts: list[str] = []

    def __call__(self, descriptor, prompt, system_prompt, **kwargs):
        import compile_memory

        if prompt.startswith(compile_memory.CRITIQUE_PROGRAM):
            self.critique_prompts.append(prompt)
            operations = json.loads(prompt.split("\nOPERATIONS\n", 1)[1].split("\n", 1)[0])
            reviews = [
                {"slug": item["slug"], "reason": "reviewed", **self.reviews.get(item["slug"], {"verdict": "pass"})}
                for item in operations
            ]
            return LLMResult(descriptor, json.dumps({"reviews": reviews}), True, None, "native")
        self.draft_prompts.append(prompt)
        return LLMResult(
            descriptor, json.dumps({"operations": self.operations, "audit": {}}), True, None, "native"
        )


def _model(
    monkeypatch: pytest.MonkeyPatch,
    *operations: dict[str, object],
    reviews: dict[str, dict[str, str]] | None = None,
) -> FakeModel:
    import compile_memory

    provider = ProviderDescriptor(
        provider="fake",
        model="fake-model",
        capabilities=MappingProxyType({"structured_output": "native", "max_tokens_enforced": True}),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=0,
        fallback_from=(),
    )
    model = FakeModel(list(operations), reviews or {})
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(compile_memory, "call_candidate", model)
    return model


def _compile() -> int:
    import compile_memory

    return compile_memory._run(argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual"))


def _notes(vault: Path) -> list[str]:
    return sorted(path.name for path in (vault / "knowledge" / "notes").glob("*.md"))


def _page(vault: Path, slug: str) -> str:
    return (vault / "knowledge" / "notes" / f"{slug}.md").read_text(encoding="utf-8")


def test_the_writer_and_the_reviewer_read_the_most_similar_note_in_full(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import compile_memory

    _search_over(vault, monkeypatch, tmp_path, vector_state="complete")
    monkeypatch.setattr(compile_memory, "SIMILAR_NOTES_MAX", 1)
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 0

    [draft] = model.draft_prompts
    [critique] = model.critique_prompts
    for prompt in (draft, critique):
        similar = prompt.split("SIMILAR NOTES", 1)[1]
        assert LEASE_BODY in similar
        assert "# Backend lease" in similar
        assert ALPHA_BODY not in prompt
        assert "The drain ran every hour." not in prompt
        assert LEDGER_TEXT not in similar
        assert EVIDENCE_TEXT not in similar
    assert draft.index("SIMILAR NOTES") < draft.index("IMMUTABLE SOURCES")
    assert UNAVAILABLE not in capsys.readouterr().err
    assert (vault / "knowledge" / "notes" / "backend-renewal.md").exists()


def test_similar_notes_are_capped_and_ranked_by_likeness(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import compile_memory

    notes = vault / "knowledge" / "notes"
    for index in range(compile_memory.SIMILAR_NOTES_MAX):
        _note_file(
            notes / f"lease-topic-{index}.md",
            "type: concept\n",
            f"Lease topic {index}",
            "A lease rule.",
            f"Lease topic {index}: the backend lease heartbeat expires " + "lease " * index,
        )
    _search_over(vault, monkeypatch, tmp_path, vector_state="complete")
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 0

    [draft] = model.draft_prompts
    similar = draft.split("SIMILAR NOTES", 1)[1].split("IMMUTABLE SOURCES", 1)[0]
    shown = [line for line in similar.splitlines() if line.startswith("### NOTE: ")]
    assert len(shown) == compile_memory.SIMILAR_NOTES_MAX
    assert ALPHA_BODY not in draft


@pytest.mark.parametrize(
    ("named", "written"),
    [("backend-lease", True), ("no-such-note", False), ("old-queue-drain", False)],
)
def test_a_duplicate_verdict_turns_a_create_into_an_update_of_the_named_live_note(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    named: str,
    written: bool,
) -> None:
    before = _notes(vault)
    lease_before = _page(vault, "backend-lease")
    _model(
        monkeypatch,
        _operation("backend-lease-expiry", "Backend lease expiry", "The lease now lasts 45 seconds."),
        reviews={"backend-lease-expiry": {"verdict": "duplicate", "duplicate_of": named}},
    )

    assert _compile() == 0

    assert _notes(vault) == before
    page = _page(vault, "backend-lease")
    err = capsys.readouterr().err
    assert f"backend-lease-expiry: the reviewer named {named}" in err
    if written:
        assert page.startswith(lease_before.rstrip())
        assert "## Update (" in page
        assert page.index(LEASE_BODY) < page.index("The lease now lasts 45 seconds.")
        assert "Backend lease expiry" not in page
    else:
        assert page == lease_before
        assert "dropped" in err


def test_two_duplicates_of_one_note_update_it_once(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lease_before = _page(vault, "backend-lease")
    _model(
        monkeypatch,
        _operation("backend-lease-expiry", "Backend lease expiry", "The lease now lasts 45 seconds."),
        _operation("backend-lease-timing", "Backend lease timing", "The lease is 45 seconds long."),
        reviews={
            "backend-lease-expiry": {"verdict": "duplicate", "duplicate_of": "backend-lease"},
            "backend-lease-timing": {"verdict": "duplicate", "duplicate_of": "backend-lease"},
        },
    )

    assert _compile() == 0

    page = _page(vault, "backend-lease")
    assert page != lease_before
    assert page.count("## Update (") == 1
    assert "The lease now lasts 45 seconds." in page
    assert "The lease is 45 seconds long." not in page


@pytest.mark.parametrize("vectors", ["no generation", "absent", "stale"])
def test_without_vectors_the_compile_reads_the_catalog_alone(
    vault: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    vectors: str,
) -> None:
    import search_memory

    if vectors == "no generation":
        monkeypatch.setattr(search_memory, "ROOT", vault)
        monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    else:
        _search_over(vault, monkeypatch, tmp_path, vector_state=vectors)
    model = _model(monkeypatch, _operation("backend-renewal", "Backend renewal", "Renew before expiry."))

    assert _compile() == 0

    [draft] = model.draft_prompts
    assert '{"slug":"backend-lease"' in draft
    assert "SIMILAR NOTES" not in draft
    assert LEASE_BODY not in draft
    assert "SIMILAR NOTES" not in model.critique_prompts[0]
    assert capsys.readouterr().err.count(UNAVAILABLE) == 1
    assert (vault / "knowledge" / "notes" / "backend-renewal.md").exists()
