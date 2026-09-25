"""The durability rules are trusted instructions and a pasted page is rejected.

Issue #20 of the readable-memory spec, Stage 3. The writer and the reviewer both
read the test "still true and useful in three months" and the never-a-note list
as part of their instructions, above the untrusted sources. A proposed body
that brings its own frontmatter, title, or evidence, claims, related or sources
section is a whole page pasted inside another: the draft is asked again, and on
the last attempt that operation is dropped while the rest of the plan is written.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import MappingProxyType

import pytest
from llm_client import LLMResult, ProviderDescriptor

DAY = "2026-07-01"
QUOTE = "The backend lease expires after 30 seconds."
RULE_LINES = (
    "still be true and useful in three months",
    "test counts and build results",
    "pull-request numbers, commit hashes, CI run links",
    "task status: in progress, tickets proposed, awaiting approval",
    "point-in-time deployment or environment state",
    "one-off setup of the owner's machine",
    "generic knowledge that any documentation already covers",
)
PASTED_PAGE = (
    "---\ntype: pattern\ntitle: \"Backend lease\"\n---\n\n"
    "The backend lease expires after 30 seconds.\n\n"
    "## Evidence\n- `knowledge/daily/2026-07-01.md` — Stated.\n"
)


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


def _operation(slug: str, body: str) -> dict[str, object]:
    return {
        "action": "create",
        "category": "patterns",
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "summary": f"What {slug} settles.",
        "body_section": "Lesson",
        "body_markdown": body,
        "evidence": [
            {"daily_date": DAY, "timestamp": "10:00:00", "quoted_text": QUOTE, "claim": "Stated."}
        ],
        "related": [],
    }


class FakeModel:
    """The fake provider: drafts one plan per call and passes every reviewed slug."""

    def __init__(self, drafts: list[list[dict[str, object]]]) -> None:
        self.drafts = drafts
        self.draft_prompts: list[str] = []
        self.critique_prompts: list[str] = []

    def __call__(self, descriptor, prompt, system_prompt, **kwargs):
        import compile_memory

        if prompt.startswith(compile_memory.CRITIQUE_PROGRAM):
            self.critique_prompts.append(prompt)
            operations = json.loads(prompt.split("\nOPERATIONS\n", 1)[1].split("\n", 1)[0])
            reviews = [{"slug": item["slug"], "verdict": "pass", "reason": "ok"} for item in operations]
            return LLMResult(descriptor, json.dumps({"reviews": reviews}), True, None, "native")
        self.draft_prompts.append(prompt)
        operations = self.drafts[min(len(self.draft_prompts), len(self.drafts)) - 1]
        return LLMResult(descriptor, json.dumps({"operations": operations, "audit": {}}), True, None, "native")


def _model(monkeypatch: pytest.MonkeyPatch, *drafts: list[dict[str, object]]) -> FakeModel:
    import compile_memory

    provider = ProviderDescriptor(
        provider="fake",
        model="fake-model",
        capabilities=MappingProxyType({"structured_output": "native", "max_tokens_enforced": True}),
        inference_settings=MappingProxyType({"max_tokens": 4000}),
        candidate_index=0,
        fallback_from=(),
    )
    model = FakeModel(list(drafts))
    monkeypatch.setattr(compile_memory, "provider_candidates", lambda *args, **kwargs: [provider])
    monkeypatch.setattr(compile_memory, "probe_candidate", lambda descriptor: True)
    monkeypatch.setattr(compile_memory, "call_candidate", model)
    return model


def _compile() -> int:
    import compile_memory

    return compile_memory._run(argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual"))


def _note(vault: Path, slug: str) -> Path:
    return vault / "knowledge" / "notes" / f"{slug}.md"


def _assert_rules_are_instructions(prompt: str, untrusted_heading: str) -> None:
    rules_at = prompt.index("DURABILITY RULES")
    untrusted_at = prompt.index(untrusted_heading)
    for line in RULE_LINES:
        assert rules_at < prompt.index(line) < untrusted_at, line


def test_the_rules_reach_the_writer_and_the_reviewer_above_the_sources(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(monkeypatch, [_operation("backend-lease", "The lease expires after 30 seconds.")])

    assert _compile() == 0

    [draft] = model.draft_prompts
    _assert_rules_are_instructions(draft, "IMMUTABLE SOURCES")
    assert draft.index("IMMUTABLE SOURCES") < draft.index(QUOTE)
    [critique] = model.critique_prompts
    _assert_rules_are_instructions(critique, "OPERATIONS")
    assert "The lease expires after 30 seconds." in _note(vault, "backend-lease").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "body",
    [
        PASTED_PAGE,
        "# Backend lease\n\nThe lease expires after 30 seconds.",
        "The lease expires after 30 seconds.\n\n## Claims\n```json\n[]\n```",
        "The lease expires after 30 seconds.\n\n## Related\n- [[alpha-queue]]",
        "The lease expires after 30 seconds.\n\n## Sources\n- the session",
    ],
    ids=["frontmatter", "title", "claims", "related", "sources"],
)
def test_a_pasted_page_is_redrafted_then_dropped_and_the_rest_is_written(
    vault: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    model = _model(
        monkeypatch,
        [_operation("backend-lease", body), _operation("backend-renewal", "Renew before expiry.")],
    )

    assert _compile() == 0

    assert len(model.draft_prompts) == 3
    assert not _note(vault, "backend-lease").exists()
    assert "Renew before expiry." in _note(vault, "backend-renewal").read_text(encoding="utf-8")
    assert "backend-lease: dropped, its body carries" in capsys.readouterr().err


def test_a_redraft_without_the_pasted_page_is_written(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _model(
        monkeypatch,
        [_operation("backend-lease", PASTED_PAGE)],
        [_operation("backend-lease", "The lease expires after 30 seconds.")],
    )

    assert _compile() == 0

    assert len(model.draft_prompts) == 2
    page = _note(vault, "backend-lease").read_text(encoding="utf-8")
    assert "The lease expires after 30 seconds." in page
    assert page.count("---\n") == 2
    assert page.count("## Evidence") == 1


def test_a_bare_body_with_subsections_rules_and_code_is_written(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "### Contract\nThe lease expires after 30 seconds.\n\n---\n\n"
        "### Verification\n```bash\n# check the lease\nstatus: live\n```\n\n"
        "```yaml\n---\ntype: example\n---\n```"
    )
    model = _model(monkeypatch, [_operation("backend-lease", body)])

    assert _compile() == 0

    assert len(model.draft_prompts) == 1
    assert body in _note(vault, "backend-lease").read_text(encoding="utf-8")
