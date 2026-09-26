# A page can supersede its own claim

Dated 2026-09-26. Files: `scripts/compile_memory.py`, `scripts/contradiction_pipeline.py`,
`scripts/claim_tree_manifest.py`, `tests/test_compile_transactions.py`,
`tests/test_a_journal_is_readable_by_every_reader.py`, `tests/test_claims.py`.

## What was seen

On the live vault every compile since the evening of 2026-09-25 failed with
`ValueError: compile claim lifecycle overlaps a compile operation target`, at night and
on every session-triggered retry. No durable note was written for those days. The
failure is deterministic: the plan is the same, so the retry is the same.

An instrumented run named the case. The batch updated a note (`replace`). One of the
new claims on that note contradicted an older, ledger-backed claim on the same note,
with overlapping validity and at least equal authority, so the policy decided
`supersede` with the note itself as the lifecycle target. The compile had already
queued its own change for that path, and `_require_unclaimed_path` refused the second
change rather than let one transaction write one path twice.

## Why the refusal was wrong

The guard is right that a transaction must not carry two changes for one path. It was
wrong to treat that as an error: a corrected fact and the fact it corrects sharing one
note is the ordinary case of an update. The same guard also refused two new notes of
one batch that each superseded a claim on the same third note: each pipeline read the
third note from disk and produced its own full after-image.

## The fix

The lifecycle planner accepts the after-images the caller's transaction already writes.
A target page found there is superseded in that image, and the page keeps the
precondition its first change set (the disk hash from the input snapshot). The compile
replaces its earlier change's content in place, keeping the change's kind and size
bound, and updates the receipt's `after_sha256`: receipt integrity checks that hash
against the committed transaction, so leaving it stale makes the receipt corrupt. A
lifecycle change that deletes a compile target is still refused.

Identity checks are unchanged: the claim to supersede must still match its fingerprint,
record hash and evidence hash, now read from the after-image, which carries the old
claim byte-for-byte because the compile only appends to a ledger.

## The second refusal: a working agent's project state

With the overlap fixed, a manual run on the live vault published two batches, sent two
to quarantine, and then failed with `persisted claim tree manifest precondition
failed`. The transaction table showed the last batch prepared four times in two
minutes, each quarantined with `precondition_failed`, while three live agent sessions
committed checkpoints to their `knowledge/projects/<project>/<repository>/state.md`
every 30–40 seconds.

The claim tree fences every page a claim could live in, and it listed `state.md`. That
file is the journal's projection: `project_journal` renders it whole on every
checkpoint, and the renderer never writes a claim ledger. It carried no claim on the
live vault. Fencing it bought nothing and made every compile that overlapped a working
agent fail. `journal.md` left the set on 2026-09-10 for the same reason; `state.md` now
follows it, and the set is `context.md` alone. The claim index and lint read the same
set, so they stop reading `state.md` for claims too.

## Side observation, not changed

After a failed session-triggered compile, `run/compile.pid` named a dead process. The
likely cause: on Windows the venv `python.exe` is a launcher, so the spawner records the
launcher's pid (it used 4.6 MB while the compile ran, which fits the launcher, not the
interpreter); when the real interpreter finishes, the launcher is still alive and the
lock is not cleared. Not verified further. `maybe_compile` clears it as stale on the next trigger; a manual
`compile_memory.py` refuses until then.
