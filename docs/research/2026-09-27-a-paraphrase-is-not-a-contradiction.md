# A paraphrase is not a contradiction

Dated 2026-09-27. Files: `scripts/contradiction_pipeline.py`, `scripts/compile_memory.py`,
`tests/test_contradiction_pipeline.py`, `tests/test_compile_transactions.py`.

## What was seen

A review of the live vault's claim quarantine, 170 candidates under `knowledge/inbox/claims/`,
re-ran the compile's deterministic policy for each one against the current claim index:

- 152 conflicted with nothing. They had been quarantined because the batch is atomic: one
  quarantined claim sends every claim of its batch to quarantine.
- 18 conflicted, and none of those was a real contradiction. In every case the new claim and
  the claim it met had been lifted from the same quoted line. Examples: `sandbox-only`
  against `for sandbox use only`, and `comma-separated` against `separated by commas`.
  Nine would have superseded their twin (same subject and relation, different value string,
  equal authority). Nine went to review because the model had named the relation differently
  (`has-state` against `has-value`).

The policy compares values as exact strings (`_same_value`). A model that words one fact
two ways produces a "contradiction" every time a day is compiled again. Since
`docs/research/2026-09-26-a-page-can-supersede-its-own-claim.md`, that contradiction could
also commit: a test compile superseded a page's own claim with its reworded copy.

## The rule

One quoted line says one thing. Two claims about the same subject whose evidence is the same
quoted bytes (`evidence.sha256`) are `equivalent`, whatever their relation or value:

- the rule sits after "different subject is unrelated", so one line may still carry facts
  about several subjects;
- it sits before the relation, interval, qualifier and value rules, so neither a renamed
  relation nor a reworded value turns the paraphrase into a review item or a supersession;
- claims from different lines keep every rule they had. A later line that states another
  value is still a contradiction.

On a `replace`, the compile also drops a new claim whose subject and evidence match an active
claim the page already holds. The page keeps one claim per line and subject, instead of
gaining a reworded copy each time the day is compiled again. The drop is named in the drop
log, like the same-id drop.

## Measured after the change

The same 170 candidates, re-assessed with the new rule: 165 conflict with nothing, and 5
still need review. The 5 are one fact stated on two different lines, the owner's Portuguese
quote and the session's English summary of it. Settling those needs meaning, and automatic
semantic supersession stays disabled by design, so they remain the reviewer's.

## Not changed

Batch atomicity: a quarantined claim still quarantines its batch. It is what turned 18
paraphrases into 170 candidates, and it is a separate decision.
