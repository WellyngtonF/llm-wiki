"""A question asked in one language finds the note written in another.

The notes are English and the questions often are not. The words of such a
question match nothing in the note, so only the vector lane can find it — and
two things kept it from doing so:

- the lexical lane dropped English and Russian function words but not
  Portuguese ones, so "de" alone matched every chunk that quoted a Portuguese
  sentence, and those rows took the top of the fusion at twice the dense weight;
- the dense lane ranked its rows by cosine times the trust weight, and fusion
  multiplied by the trust weight again. Cosines of one multilingual model sit in
  a narrow band, so a trusted page about something else took the dense lane's
  first place from the page the question was about.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

np = pytest.importorskip("numpy")

# One dimension per concept, named in both languages, plus a constant every
# text shares: the narrow cosine band a real multilingual model produces.
CONCEPTS = (
    ("vehicle", "veículo"),
    ("catalog", "catálogo"),
    ("delet", "exclus", "apaga"),
    ("recommend", "recomenda"),
    ("login",),
    ("payment", "pagamento"),
)
SHARED = 2.0

QUESTION = "exclusão de veículo do catálogo apaga recomendações"
TARGET = "knowledge/notes/catalog-vehicle-deletion.md"


def _embed(texts: list[str]) -> object:
    rows = []
    for text in texts:
        lowered = str(text).casefold()
        concepts = [float(any(word in lowered for word in names)) for names in CONCEPTS]
        row = np.array([SHARED, *concepts], dtype=np.float32)
        rows.append(row / float(np.linalg.norm(row)))
    return np.vstack(rows)


def _page(path: Path, frontmatter: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")


def _target(notes: Path) -> None:
    _page(
        notes / "catalog-vehicle-deletion.md",
        "type: decision\nsource_authority: ai-derived\nconfidence: high",
        "# Deleting a catalog vehicle also deletes its recommendations\n\n"
        "One-sentence summary: removing a vehicle cascades to what points at it.\n\n"
        "## Decision\nA vehicle removed from the catalog takes every recommendation "
        "that names it along.\n",
    )


def _lexical_noise(vault: Path) -> None:
    """Chunks that share one Portuguese function word with the question, nothing else."""
    _page(
        vault / "knowledge" / "notes" / "login-screen-review.md",
        "type: pattern\nsource_authority: ai-derived\nconfidence: medium",
        "# Login screen review\n\nOne-sentence summary: how the login screen is reviewed.\n\n"
        "## Evidence\n- The owner asked: \"revisar a tela de login antes de publicar\".\n",
    )
    for slug in ("alpha", "backend"):
        _page(
            vault / "knowledge" / "projects" / slug / "state.md",
            "type: project-state",
            f"# {slug} - State\n\n## Current task\nRevisar o fluxo de pagamento de pedidos "
            "de cada cliente de teste.\n",
        )


def _trusted_elsewhere(notes: Path) -> None:
    """A page the owner stated, about something the question only brushes."""
    _page(
        notes / "owner-login-rule.md",
        "type: decision\nsource_authority: user\nconfidence: high",
        "# Login sessions expire after a day\n\n"
        "One-sentence summary: the owner's rule for login sessions.\n\n"
        "## Decision\nA login session expires after a day, whichever vehicle of the "
        "catalog it was opened on.\n",
    )


def _catalog_over(tmp_path: Path, vault: Path, monkeypatch: pytest.MonkeyPatch):
    import search_memory
    from corpus_snapshot import collect_corpus
    from repository_scope import resolve_repository_scope

    notes = vault / "knowledge" / "notes"
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    snapshot = collect_corpus(vault)
    catalog = search_memory.GenerationCatalog(tmp_path / "state")
    generation = catalog.generations_path / "gen-cross-language"
    generation.mkdir(parents=True)
    artifacts = [search_memory.build_generation_fts(snapshot, generation)]
    artifacts.extend(
        search_memory.build_generation_numpy_vectors(
            snapshot,
            generation,
            embedder=_embed,
            model_id="m",
            model_revision="r1",
            dimensions=1 + len(CONCEPTS),
        )
    )
    manifest = {
        "generation_id": "gen-cross-language",
        "schema_version": "corpus-generation/v1",
        "collector_version": snapshot.collector_version,
        "extractor_version": snapshot.extractor_version,
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "embedding_model_id": "m",
        "embedding_model_revision": "r1",
        "vector_dimensions": 1 + len(CONCEPTS),
        "source_manifest_sha256": snapshot.corpus_sha256,
        "artifacts": artifacts,
        "vector_state": "complete",
        "repository_scope": resolve_repository_scope(vault).as_dict(),
    }

    class Catalog:
        generations_path = catalog.generations_path

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return manifest

    return Catalog()


def _search(catalog: object, query: str) -> list[dict]:
    import search_memory

    return search_memory.search(
        query,
        semantic=True,
        catalog=catalog,
        generation_embedder=_embed,
        generation_model_id="m",
        generation_model_revision="r1",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )


def test_the_note_comes_before_chunks_that_share_only_a_function_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    _target(vault / "knowledge" / "notes")
    _lexical_noise(vault)

    hits = _search(_catalog_over(tmp_path, vault, monkeypatch), QUESTION)

    assert hits
    assert hits[0]["path"] == TARGET
    assert all(hit["bm25_rank"] is None for hit in hits)


def test_the_dense_lane_ranks_by_similarity_and_trust_weighs_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page the owner stated keeps its fusion weight, not a second one in the lane."""
    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    _target(notes)
    _trusted_elsewhere(notes)

    hits = _search(_catalog_over(tmp_path, vault, monkeypatch), QUESTION)

    best_rank: dict[str, int] = {}
    for hit in hits:
        if hit["vector_rank"] is not None:
            best_rank[hit["path"]] = min(hit["vector_rank"], best_rank.get(hit["path"], hit["vector_rank"]))
    assert best_rank[TARGET] == 1
    assert best_rank["knowledge/notes/owner-login-rule.md"] > 1
