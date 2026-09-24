"""Unrelated lexical matches must not quarantine a new grounded subject."""
import json

import pytest
from contradiction_pipeline import ContradictionPipeline

from tests.test_contradiction_pipeline import claim


def _review(*labels):
    return json.dumps({"reviews": [
        {"index": index, "relevance": label, "confidence": "high"}
        for index, label in enumerate(labels)
    ]})


def test_two_unrelated_reviews_allow_new_claim_without_touching_legacy_pages():
    from contradiction_pipeline import review_secondary_context

    hits = [{"path": "knowledge/notes/preferences.md", "content": "Use short paragraphs."}]
    calls = []

    def call(prompt, system):
        calls.append((prompt, system))
        return _review("unrelated")

    pipeline = ContradictionPipeline(
        evaluators=(),
        secondary_search=lambda query, limit: review_secondary_context(query, hits, call=call),
    )
    result = pipeline.assess(claim("red"), commit=False)
    assert result.recommendation == "keep-both"
    assert result.lifecycle_mutations == ()
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]


@pytest.mark.parametrize("second", [
    _review("possibly_related"), "invalid JSON", _review(),
    '{"reviews":[{"index":0,"relevance":"unrelated","confidence":"low"}]}',
])
def test_uncertainty_disagreement_or_invalid_review_keeps_quarantine(second):
    from contradiction_pipeline import review_secondary_context

    hits = [{"path": "knowledge/notes/legacy.md", "content": "The project state is blue."}]
    replies = iter([_review("unrelated"), second])
    context = review_secondary_context("The project state is red.", hits,
                                       call=lambda *args: next(replies))
    assert context == hits


def test_related_hit_survives_even_when_another_hit_is_unrelated():
    from contradiction_pipeline import review_secondary_context

    hits = [{"content": "Unrelated preferences"}, {"content": "Project state is blue"}]
    assert review_secondary_context("Project state is red", hits,
        call=lambda *args: _review("unrelated", "possibly_related")) == [hits[1]]


def test_full_legacy_note_can_coexist_with_new_account_preference(tmp_path):
    from contradiction_pipeline import review_secondary_context

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    original = "# Deployment history\nThe OMS deployment failed on September 10.\n"
    page = notes / "history.md"
    page.write_bytes(original.encode("utf-8"))
    hits = [{"path": "knowledge/notes/history.md", "content": "truncated search chunk"}]
    prompts = []

    def review(prompt, system):
        prompts.append(json.loads(prompt))
        return _review("compatible")

    assert review_secondary_context("Use the Cantu account for OMS GitHub access.",
        hits, root=tmp_path, call=review) == []
    assert len(prompts) == 2
    assert all(p["notes"][0]["text"] == original for p in prompts)
    assert page.read_text(encoding="utf-8") == original


def test_full_note_conflict_is_not_hidden_by_a_search_chunk(tmp_path):
    from contradiction_pipeline import review_secondary_context

    notes = tmp_path / "knowledge/notes"
    notes.mkdir(parents=True)
    (notes / "accounts.md").write_text("Use only the personal account for OMS.", encoding="utf-8")
    hits = [{"path": "knowledge/notes/accounts.md", "content": "OMS account setup"}]
    assert review_secondary_context("Use the Cantu account for OMS.", hits, root=tmp_path,
        call=lambda *args: _review("possibly_related")) == hits


def test_missing_or_outside_full_note_cannot_clear_hold(tmp_path):
    from contradiction_pipeline import review_secondary_context

    for path in ["knowledge/notes/missing.md", "../../outside.md"]:
        hits = [{"path": path, "content": "unrelated snippet"}]
        assert review_secondary_context("new fact", hits, root=tmp_path,
            call=lambda *args: pytest.fail("must not review an unavailable full note")) == hits
