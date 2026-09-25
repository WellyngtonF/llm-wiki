"""The weekly consolidation rewrites a note's prose and keeps exactly one claims ledger.

The body sent to the model held the Claims ledger and the history block kept the
original `## Claims`: a consolidated note carried two Claims headings, and every
ledger reader refused it. Spec: `docs/specs/2026-09-24-readable-memory.md`, Stage 5.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from tests.test_claims import raw_claim, source_bytes  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Own interpreter: the weekly pass resolves the vault and runtime roots at import time.
# The fake provider answers; every prompt it is given is kept for the assertions.
WEEKLY = """
import json, sys
sys.path.insert(0, sys.argv[1])
import llm_client
answer = llm_client._BACKENDS["fake"]
prompts = []
def recorded(descriptor, prompt, *args, **kwargs):
    prompts.append(prompt)
    return answer(descriptor, prompt, *args, **kwargs)
llm_client._BACKENDS["fake"] = recorded
import scheduled_weekly
lines = []
scheduled_weekly._reflect(lines.append)
print(json.dumps({"prompts": prompts, "lines": lines}))
"""

REWRITE = (
    "# Alpha Service\n\nOne-sentence summary: alpha serves the backend and moved twice.\n\n"
    + " ".join(["The consolidated narrative keeps every fact the updates stated."] * 8)
    + "\n"
)
PROSE = (
    "# Alpha Service\n\nOne-sentence summary: alpha serves the backend.\n\n"
    "Alpha answers requests for the backend.\n"
)
UPDATES = "\n\n## Update (2026-02-01)\nAlpha moved to region one.\n\n## Update (2026-03-01)\nAlpha moved again.\n"


def _claim(tmp_path: Path, claim_id: str) -> dict:
    from claims import ClaimPipeline
    from evidence_resolver import EvidenceResolver

    daily = tmp_path / "claims-source/knowledge/daily/2026-01-02.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_bytes(source_bytes())
    pipeline = ClaimPipeline(EvidenceResolver(tmp_path / "claims-source"))
    block = pipeline.split_blocks(source_bytes())[0]
    record = pipeline.normalize(pipeline.verify_literal(pipeline.extract(block, raw_claim())[0])).record
    return {**record, "id": claim_id}


def _ledger(*claims: dict) -> str:
    ledger = {"schema_version": "claim-ledger/v1", "claims": list(claims)}
    encoded = json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"\n\n## Claims\n```json\n{encoded}\n```\n"


def _vault(tmp_path: Path, pages: dict[str, str]) -> Path:
    notes = tmp_path / "vault/knowledge/notes"
    notes.mkdir(parents=True)
    (tmp_path / "state/run").mkdir(parents=True)
    for name, text in pages.items():
        (notes / name).write_text(text, encoding="utf-8")
    return notes


def _weekly(tmp_path: Path) -> dict:
    environment = {
        **os.environ,
        "LLM_WIKI_ROOT": str(tmp_path / "vault"),
        "LLM_WIKI_STATE_ROOT": str(tmp_path / "state"),
        "MEMORY_LLM_PROVIDER": "fake",
        "MEMORY_LLM_FAKE_RESPONSE": REWRITE,
    }
    done = subprocess.run(
        [sys.executable, "-c", WEEKLY, str(SCRIPTS)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _claim_ids(page: Path) -> list[str]:
    from claims import parse_claim_ledger

    ledger = parse_claim_ledger(page.read_bytes())
    assert ledger is not None
    return [str(record["id"]) for record in ledger["claims"]]


def _history(text: str) -> str:
    return text.split("<details>", 1)[1].split("</details>", 1)[0]


def test_a_note_with_two_updates_becomes_one_page_with_one_ledger(tmp_path):
    claim = _claim(tmp_path, "claim:alpha-0")
    note = "---\ntype: concept\n---\n\n" + PROSE + _ledger(claim) + UPDATES
    decision = note.replace("type: concept", "type: decision")
    notes = _vault(tmp_path, {"alpha-service.md": note, "alpha-decision.md": decision})

    run = _weekly(tmp_path)

    text = (notes / "alpha-service.md").read_text(encoding="utf-8")
    assert [("## Claims" in prompt, "claim-ledger/v1" in prompt) for prompt in run["prompts"]] == [(False, False)]
    assert "One-sentence summary: alpha serves the backend and moved twice." in text
    assert (text.count("<details>"), text.count("## Claims")) == (1, 1)
    assert "Alpha moved again." in _history(text) and "## Claims" not in _history(text)
    assert text.rstrip().endswith("```") and text.rsplit("\n## ", 1)[1].startswith("Claims")
    assert _claim_ids(notes / "alpha-service.md") == ["claim:alpha-0"]
    assert (notes / "alpha-decision.md").read_text(encoding="utf-8") == decision


def test_a_note_the_old_pass_left_with_two_ledgers_ends_with_one_merged_ledger(tmp_path):
    kept, later = _claim(tmp_path, "claim:alpha-0"), _claim(tmp_path, "claim:alpha-1")
    old_history = (
        "\n\n## History (pre-reflection 2026-01-10)\n<details>\n"
        "<summary>Original page before reflection</summary>\n\n"
        + PROSE + _ledger(kept) + "\n\n</details>\n"
    )
    note = "---\ntype: concept\n---\n\n" + REWRITE + UPDATES + _ledger(later) + old_history
    notes = _vault(tmp_path, {"alpha-service.md": note})

    _weekly(tmp_path)

    text = (notes / "alpha-service.md").read_text(encoding="utf-8")
    assert (text.count("<details>"), text.count("## Claims")) == (2, 1)
    assert sorted(_claim_ids(notes / "alpha-service.md")) == ["claim:alpha-0", "claim:alpha-1"]


def test_updates_compile_appends_after_the_ledger_are_consolidated_next_week(tmp_path):
    claim = _claim(tmp_path, "claim:alpha-0")
    notes = _vault(tmp_path, {"alpha-service.md": "---\ntype: concept\n---\n\n" + PROSE + _ledger(claim) + UPDATES})
    _weekly(tmp_path)
    page = notes / "alpha-service.md"
    later = "\n\n## Update (2026-10-01)\nAlpha moved back.\n\n## Update (2026-11-01)\nAlpha stayed.\n"
    page.write_text(page.read_text(encoding="utf-8").rstrip() + later, encoding="utf-8")

    run = _weekly(tmp_path)

    text = page.read_text(encoding="utf-8")
    assert (len(run["prompts"]), text.count("<details>"), text.count("## Claims")) == (1, 2, 1)
    assert "## Update (" not in text.split("## History (pre-reflection", 1)[0]
    assert "Alpha stayed." in _history(text)
    assert _claim_ids(page) == ["claim:alpha-0"]


def test_a_note_whose_ledgers_disagree_on_a_claim_is_left_as_it_was(tmp_path):
    claim = _claim(tmp_path, "claim:alpha-0")
    old_history = (
        "\n\n## History (pre-reflection 2026-01-10)\n<details>\n\n"
        + PROSE + _ledger({**claim, "confidence": "low"}) + "\n\n</details>\n"
    )
    note = "---\ntype: concept\n---\n\n" + REWRITE + UPDATES + _ledger(claim) + old_history
    notes = _vault(tmp_path, {"alpha-service.md": note})

    run = _weekly(tmp_path)

    assert (run["prompts"], (notes / "alpha-service.md").read_text(encoding="utf-8")) == ([], note)
    assert "not reflected — claim claim:alpha-0 differs between the Claims ledgers" in run["lines"][-1]
