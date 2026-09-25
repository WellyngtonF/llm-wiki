"""Search finds a note by its prose, never by its claims ledger.

The ledger is one line of JSON that repeats claim text copied from daily logs,
evidence references and hashes. Indexed as a chunk of its own, it outranked the
prose of the note a query was about in both lanes, on the lexical words it
copied and on the vector those words made. Its readers — the claim index and
the contradiction pipeline — read the page file, not the search index.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

np = pytest.importorskip("numpy")

LEDGER_ONLY = "zephyrquartz"
VOCABULARY = ("lease", "heartbeat", "backend", "expires", "release", "owner", "signed", LEDGER_ONLY)
LEASE_PROSE = "A heartbeat renews the backend lease before it expires; a missed renewal frees it."
RELEASE_PROSE = "Releases are signed off by their owner on the release branch."
# The release note's ledger copied the lease discussion from the daily log, many times over.
COPIED = "The backend lease heartbeat expires; the backend lease heartbeat renews. "


def _embed(texts: list[str]) -> object:
    rows = []
    for text in texts:
        lowered = str(text).casefold()
        row = np.array([lowered.count(word) for word in VOCABULARY], dtype=np.float32)
        norm = float(np.linalg.norm(row))
        rows.append(row / norm if norm else np.full(len(VOCABULARY), 1e-3, dtype=np.float32))
    return np.vstack(rows)


def _note(path: Path, title: str, prose: str, ledger_text: str) -> None:
    path.write_text(
        f"---\ntype: pattern\n---\n\n# {title}\n\nOne-sentence summary: {title}.\n\n"
        f"## Lesson\n{prose}\n\n"
        f"## Evidence\n- `daily:2026-06-01 sha256:{'a' * 64} block:09:00:00 bytes:0-10` — Stated.\n\n"
        "## Claims\n```json\n"
        f'{{"claims":[{{"id":"claim-1","text":"{ledger_text}"}}],"schema_version":"claim-ledger/v1"}}\n'
        "```\n",
        encoding="utf-8",
    )


@pytest.fixture
def search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Search over an active evidence generation of a two-note vault."""
    import search_memory
    from corpus_snapshot import collect_corpus
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    _note(notes / "backend-lease.md", "Backend lease", LEASE_PROSE, "The lease is renewed.")
    _note(notes / "alpha-release-owner.md", "Alpha release owner", RELEASE_PROSE, COPIED * 6 + LEDGER_ONLY)
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    snapshot = collect_corpus(vault)
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-ledger"
    generation.mkdir(parents=True)
    artifacts = [search_memory.build_generation_fts(snapshot, generation)]
    artifacts.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=_embed,
            model_id="m",
            model_revision="r1",
            dimensions=len(VOCABULARY),
        )
    )
    manifest = {
        "generation_id": "gen-ledger",
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": "m",
        "embedding_model_revision": "r1",
        "vector_dimensions": len(VOCABULARY),
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": artifacts,
        "vector_state": "complete",
        "repository_scope": resolve_repository_scope(vault).as_dict(),
    }

    class Catalog:
        generations_path = catalog.generations_path

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return manifest

    def run(query: str) -> list[dict]:
        return search_memory.search(
            query,
            semantic=True,
            catalog=Catalog(),
            generation_embedder=_embed,
            generation_model_id="m",
            generation_model_revision="r1",
            graph=False,
            rerank=False,
            emit_telemetry=False,
        )

    return run


def _text(hit: dict) -> str:
    return str(hit.get("content") or hit.get("summary") or "")


def _is_ledger(hit: dict) -> bool:
    return "Claims" in (hit.get("heading_ancestry") or []) or '"claims"' in _text(hit)


def test_a_word_only_the_ledger_holds_finds_no_ledger(search) -> None:
    hits = search(LEDGER_ONLY)

    assert all(LEDGER_ONLY not in _text(hit) for hit in hits)
    assert not any(_is_ledger(hit) for hit in hits)


def test_the_prose_that_answers_comes_first(search) -> None:
    hits = search("backend lease heartbeat expires")

    assert hits
    assert hits[0]["path"] == "knowledge/notes/backend-lease.md"
    assert LEASE_PROSE in _text(hits[0])
    assert not any(_is_ledger(hit) for hit in hits)
