"""The compile tags each note with the modules it is about.

Issue #22 of the readable-memory spec, Stage 3. The draft reads, for every project
the batch's daily entries name, the module tags that project's live notes already
use, and the tags of notes without a project for work in no registered
repository. It proposes tags for each note; the compile normalises them to
lowercase kebab-case, drops the project's own name, dedupes and caps them, writes
them as `tags:` on a new note and appends them to an updated note's existing
`tags:`. Every tag its project did not use before is named in the vault log line
of that compile. Tags never enter the claims ledger.

Each test runs the compile with a fake model and reads what a person would open:
the note, the vault log, and the prompt the model was given.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml
from llm_client import LLMResult, ProviderDescriptor

DAY = "2026-07-01"
LESSON = "The backend lease now expires after 45 seconds."
NOTES = Path("knowledge/notes")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import compile_memory
    import memory_state

    vault = tmp_path / "vault"
    for relative in ("knowledge/daily", "knowledge/notes", "knowledge/projects"):
        (vault / relative).mkdir(parents=True)
    (vault / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    (vault / "knowledge" / "index.md").write_text("# idx\n", encoding="utf-8")
    (vault / "knowledge" / "log.md").write_text("# Session Memory Log\n", encoding="utf-8")
    backend = (tmp_path / "work" / "backend").as_posix()
    (vault / "knowledge/projects/project-map.md").write_text(
        f"# Project map\n\n## product-a\n\n- {backend}\n\n## product-b\n\n"
        f"- {(tmp_path / 'work' / 'web').as_posix()}\n",
        encoding="utf-8",
    )
    _note(vault, "backend-lease", 'project: "product-a"\ntags: [backend, queue]\n')
    _note(vault, "web-routing", 'project: "product-b"\ntags: [router]\n')
    _note(vault, "shell-aliases", "tags:\n  - shell\n")
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
    monkeypatch.setattr(compile_memory, "DAILY_DIR", vault / "knowledge" / "daily")
    monkeypatch.setattr(compile_memory, "KNOWLEDGE", vault / NOTES)
    monkeypatch.setattr(compile_memory, "AGENTS", vault / "AGENTS.md")
    monkeypatch.setattr(compile_memory, "INDEX", vault / "knowledge" / "index.md")
    monkeypatch.setattr(compile_memory, "LOG", vault / "knowledge" / "log.md")
    monkeypatch.setattr(memory_state, "ROOT", vault)
    monkeypatch.setattr(memory_state, "STATE_ROOT", state_root)
    monkeypatch.setattr(memory_state, "STATE_DIR", state_root / "run")
    monkeypatch.setattr(memory_state, "STATE_FILE", state_root / "run" / "state.json")
    monkeypatch.setattr(memory_state, "REPORTS_DIR", state_root / "logs")
    return vault


def _note(vault: Path, slug: str, frontmatter: str) -> None:
    title = slug.replace("-", " ").capitalize()
    (vault / NOTES / f"{slug}.md").write_text(
        f"---\ntype: pattern\ntitle: \"{title}\"\n{frontmatter}---\n\n# {title}\n\n"
        f"One-sentence summary: What {slug} settles.\n\n## Lesson\nSettled earlier.\n",
        encoding="utf-8",
    )


def _daily(vault: Path, *, repository: str | None) -> None:
    location = f"- Repository: `{repository}`\n" if repository else ""
    (vault / "knowledge/daily" / f"{DAY}.md").write_text(
        f"# Daily log {DAY}\n\n## [10:00:00] session-end | session-1000\n"
        f"- Trigger: `session-end`\n{location}- Tier: `durable`\n\n{LESSON}\n",
        encoding="utf-8",
    )


def _registered(vault: Path) -> None:
    _daily(vault, repository=(vault.parent / "work" / "backend").as_posix())


def _operation(action: str, slug: str, tags: object) -> dict[str, object]:
    return {
        "action": action,
        "category": "patterns",
        "slug": slug,
        "title": "Lease expiry",
        "summary": "The backend lease expires after 45 seconds.",
        "body_section": "Lesson",
        "body_markdown": "Renew the lease before 45 seconds pass.",
        "evidence": [
            {"daily_date": DAY, "timestamp": "10:00:00", "quoted_text": LESSON, "claim": "Stated."}
        ],
        "related": [],
        "tags": tags,
        "claims": [
            {
                "evidence_index": 0,
                "subject": "backend lease expiry",
                "relation": "has-value",
                "value": {"type": "number", "value": "45", "unit": "seconds"},
            }
        ],
    }


class FakeModel:
    def __init__(self, operations: list[dict[str, object]]) -> None:
        self.operations = operations
        self.draft_prompts: list[str] = []

    def __call__(self, descriptor, prompt, system_prompt, **kwargs):
        import compile_memory

        if prompt.startswith(compile_memory.CRITIQUE_PROGRAM):
            operations = json.loads(prompt.split("\nOPERATIONS\n", 1)[1].split("\n", 1)[0])
            reviews = [{"slug": item["slug"], "verdict": "pass", "reason": "ok"} for item in operations]
            return LLMResult(descriptor, json.dumps({"reviews": reviews}), True, None, "native")
        self.draft_prompts.append(prompt)
        return LLMResult(
            descriptor, json.dumps({"operations": self.operations, "audit": {}}), True, None, "native"
        )


def _compile(monkeypatch: pytest.MonkeyPatch, *operations: dict[str, object]) -> FakeModel:
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
    arguments = argparse.Namespace(all=False, file=None, dry_run=False, trigger="manual")
    assert compile_memory._run(arguments) == 0
    return model


def _frontmatter(vault: Path, slug: str) -> dict[str, object]:
    page = (vault / NOTES / f"{slug}.md").read_text(encoding="utf-8")
    return yaml.safe_load(page.split("---\n")[1])


def _page(vault: Path, slug: str) -> str:
    return (vault / NOTES / f"{slug}.md").read_text(encoding="utf-8")


def _log(vault: Path) -> str:
    return (vault / "knowledge" / "log.md").read_text(encoding="utf-8")


def _module_tags_block(prompt: str) -> str:
    return prompt.split("MODULE TAGS", 1)[1].split("\n\n", 1)[0]


def test_the_draft_reads_the_tags_of_the_project_its_entries_name(vault, monkeypatch):
    _registered(vault)

    model = _compile(monkeypatch, _operation("create", "lease-expiry", ["queue"]))

    [prompt] = model.draft_prompts
    block = _module_tags_block(prompt)
    assert "- product-a: backend, queue" in block
    assert "router" not in block  # product-b is not in this batch
    assert "shell" not in block  # nor is work outside every project
    assert "reuse" in block.casefold()
    assert prompt.index("MODULE TAGS") < prompt.index("IMMUTABLE SOURCES")


def test_a_new_note_gets_normalised_tags_without_its_project_name(vault, monkeypatch):
    _registered(vault)
    proposed = ["Backend Lease", "product-a", "queue", "QUEUE", "x" * 41, 7]

    _compile(monkeypatch, _operation("create", "lease-expiry", proposed))

    frontmatter = _frontmatter(vault, "lease-expiry")
    assert frontmatter["project"] == "product-a"
    assert frontmatter["tags"] == ["backend-lease", "queue"]
    assert "\ntags: [backend-lease, queue]\n" in _page(vault, "lease-expiry")


def test_tags_stay_out_of_the_claims_ledger(vault, monkeypatch):
    _registered(vault)

    _compile(monkeypatch, _operation("create", "lease-expiry", ["lease-renewal"]))

    ledger = _page(vault, "lease-expiry").split("## Claims", 1)[1]
    assert "backend lease expiry" in ledger
    assert "lease-renewal" not in ledger


def test_an_update_appends_new_tags_after_the_existing_ones(vault, monkeypatch):
    _registered(vault)
    before = _page(vault, "backend-lease")

    _compile(monkeypatch, _operation("update", "backend-lease", ["queue", "lease-renewal", "product-a"]))

    after = _page(vault, "backend-lease")
    assert _frontmatter(vault, "backend-lease")["tags"] == ["backend", "queue", "lease-renewal"]
    assert after.replace("tags: [backend, queue, lease-renewal]", "tags: [backend, queue]").startswith(
        before.split("\n---\n", 1)[0]
    )
    assert "## Update (" in after


def test_an_update_to_a_block_list_keeps_every_existing_tag(vault, monkeypatch):
    _daily(vault, repository=None)

    _compile(monkeypatch, _operation("update", "shell-aliases", ["shell", "prompt-theme"]))

    assert _frontmatter(vault, "shell-aliases")["tags"] == ["shell", "prompt-theme"]


def test_an_update_with_nothing_new_leaves_the_frontmatter_alone(vault, monkeypatch):
    _registered(vault)
    header = _page(vault, "backend-lease").split("\n---\n", 1)[0]

    _compile(monkeypatch, _operation("update", "backend-lease", ["Queue", "backend"]))

    assert _page(vault, "backend-lease").startswith(header + "\n---\n")
    assert "New tags" not in _log(vault)


def test_every_tag_new_to_its_project_is_named_in_the_vault_log(vault, monkeypatch):
    _registered(vault)

    _compile(monkeypatch, _operation("create", "lease-expiry", ["router", "queue"]))

    [line] = [line for line in _log(vault).splitlines() if "compile completed" in line]
    # `router` is product-b's tag, so it is new to product-a; `queue` is not.
    assert line.endswith(" New tags: router (product-a).")


def test_notes_from_unregistered_work_are_tagged_from_the_no_project_pool(vault, monkeypatch):
    _daily(vault, repository=None)

    model = _compile(monkeypatch, _operation("create", "lease-expiry", ["shell", "lease-renewal"]))

    block = _module_tags_block(model.draft_prompts[0])
    assert "- (no project): shell" in block
    assert "product-a" not in block
    frontmatter = _frontmatter(vault, "lease-expiry")
    assert "project" not in frontmatter
    assert frontmatter["tags"] == ["shell", "lease-renewal"]
    assert "New tags: lease-renewal (no project)." in _log(vault)
