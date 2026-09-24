import re

import pytest
from claims import parse_claim_ledger
from evidence_resolver import MAX_DAILY_PART_BYTES, _daily_part_bounds, compile_part_slice
from reliable_memory import canonical_json_bytes, sha256_bytes

from tests.test_contradiction_pipeline import claim


def test_timestamp_only_entries_split_without_losing_bytes():
    content = b"# 2026-09-17\n" + b"".join(
        f"\n## [10:{index:02d}:00] session-end | codex\n".encode() + b"fact " * 1300
        for index in range(6)
    )
    bounds = _daily_part_bounds(content)
    assert all(end - start <= MAX_DAILY_PART_BYTES for start, end in bounds)
    assert b"".join(content[start:end] for start, end in bounds) == content


def test_old_marker_based_part_remains_resolvable_after_new_splitting():
    first = b"<!-- llm-wiki-operation:first -->\n" + b"a" * 9000 + b"\n"
    second = b"<!-- llm-wiki-operation:second -->\n" + b"b" * 7000 + b"\n"
    third = b"<!-- llm-wiki-operation:third -->\n" + b"".join(
        f"## [11:{i:02d}:00] session-end\n".encode() + b"c" * 6500 + b"\n"
        for i in range(3)
    )
    content = first + second + third
    assert compile_part_slice(content, sha256_bytes(third)) == third


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\r\n\r\n"])
def test_claim_ledger_allows_blank_lines_before_related_section(separator):
    ledger = {"schema_version": "claim-ledger/v1", "claims": [dict(claim().record)]}
    content = b"## Claims\n```json\n" + canonical_json_bytes(ledger) + b"\n```"
    assert parse_claim_ledger(content + separator.encode() + b"## Related\n- [[other]]\n") == ledger


def test_claim_ledger_still_rejects_unstructured_trailing_claim_text():
    ledger = {"schema_version": "claim-ledger/v1", "claims": [dict(claim().record)]}
    content = b"## Claims\n```json\n" + canonical_json_bytes(ledger) + b"\n```\n\nUnvalidated extra claim\n"
    with pytest.raises(ValueError):
        parse_claim_ledger(content)


def test_draft_knows_existing_targets_even_when_optional_context_is_omitted():
    import compile_memory as compiler

    target = compiler.TargetSnapshot("knowledge/notes/existing-rule.md", b"old rule", sha256_bytes(b"old rule"))
    inputs = compiler.CompileInputs((), (), (target,))
    prompt = compiler._draft_prompt(inputs)
    assert "knowledge/notes/existing-rule.md" in prompt
    assert "Never create a listed path" in prompt


def test_every_receipted_daily_part_is_visible_to_the_draft(tmp_path, monkeypatch):
    import compile_memory as compiler

    root = tmp_path
    (root / "knowledge/daily").mkdir(parents=True)
    daily = root / "knowledge/daily/2026-09-17.md"
    daily.write_bytes(b"# 2026-09-17\n" + b"".join(
        f"<!-- llm-wiki-operation:{i} -->\n## [10:{i:02d}:00] session-end\nPART-{i}\n".encode()
        + b"detail " * 2100 + b"\n" for i in range(4)
    ))
    monkeypatch.setattr(compiler, "ROOT", root)
    monkeypatch.setattr(compiler, "KNOWLEDGE", root / "knowledge/notes")
    for name in ("AGENTS", "INDEX", "LOG"):
        monkeypatch.setattr(compiler, name, root / (name + ".md"))
    inputs = compiler.snapshot_compile_inputs([daily])
    batches = compiler.pack_compile_batches(inputs, model=None)
    for batch in batches:
        prompt = compiler._draft_prompt(batch.inputs)
        for part in batch.inputs.dailies:
            unlabelled = re.sub(r"(?m)^\[@E\d+\] ", "", prompt)
            assert part.content.decode() in unlabelled, "receipt would acknowledge unseen source bytes"
    assert len(batches) > 1


def test_source_selector_binds_original_words_and_actual_timestamp(tmp_path, monkeypatch):
    import compile_memory as compiler

    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    content = (b"## [14:07:52] session-end\n\n## [14:08:27] session-end\n"
               b"Use one endpoint and a ready-for-agent specification.\n")
    daily = compiler.DailySnapshot("knowledge/daily/2026-09-17.md", content, sha256_bytes(content))
    inputs = compiler.CompileInputs((daily,), (), ())
    operations = [{"evidence": [{"quoted_text": "@E0", "daily_date": "2026-09-16",
                                "timestamp": "14:07:52", "claim": "Use one endpoint."}]}]
    compiler._resolve_source_line_selectors(operations, inputs)
    evidence = operations[0]["evidence"][0]
    assert evidence["quoted_text"] == "Use one endpoint and a ready-for-agent specification."
    assert evidence["timestamp"] == "14:08:27"
    assert evidence["daily_date"] == "2026-09-17"
    assert "block:14:08:27" in compiler._evidence_binding(evidence, inputs)["reference"]


def test_unknown_selector_is_not_silently_dropped():
    import compile_memory as compiler

    with pytest.raises(ValueError, match="unknown source-line selector"):
        compiler._resolve_source_line_selectors(
            [{"evidence": [{"quoted_text": "@E999"}]}], compiler.CompileInputs((), (), ())
        )
