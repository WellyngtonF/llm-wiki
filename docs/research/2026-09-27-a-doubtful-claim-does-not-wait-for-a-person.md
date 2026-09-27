# A doubtful claim does not wait for a person

Dated 2026-09-27. The owner's decision, departing from the upstream's review-first design:
durable notes are generated with as little human intervention as possible. Files:
`scripts/compile_memory.py`, `scripts/contradiction_pipeline.py`, `docs/USER-GUIDE.md`,
`docs/operating-model.md`, `tests/test_compile_transactions.py`.

## What was seen

A claim the contradiction policy could not settle sent its whole batch to
`knowledge/inbox/claims/`: every claim of the batch was written there as a candidate, no page
was published, and the daily stayed pending "until the candidate is reviewed". No command
accepted a candidate. It took a person publishing the decision by hand.

The live vault held 170 candidates, and nobody had worked the queue. Its review
(`docs/research/2026-09-27-a-paraphrase-is-not-a-contradiction.md`) found no real
contradiction among them. 152 were batch siblings of the claim that caused the quarantine.
18 were paraphrases, 13 of them from the same line and 5 restating a held fact from another
line. One whole decision page, the Payment Code rules of 2026-09-17, had never been written.

## The decision

1. **A doubtful claim rides on its page.** A claim whose recommendation is `quarantine` is
   written into its page's ledger with `lifecycle: quarantined`. The ledger rendering already
   did this; the batch diversion had made that path unreachable. Retrieval and the
   contradiction index read active claims only, so the claim is kept but asserts nothing. The
   page and the rest of the batch publish, the daily compiles, and the compile prints how many
   doubtful claims it kept. The compile writes no candidate file. The standalone contradiction
   check (`check_contradiction`) can still write one.
2. **The model may settle, never supersede.** Where the deterministic rules leave a claim
   unresolved, two agreeing, supported, high-confidence evaluations decide `compatible`
   (keep both) or `refinement`. This is the calibrated path the pipeline already had behind
   `benchmark_gate`, and the compile now opens it. A semantic `contradiction` still never
   supersedes anything: the claim stays doubtful and the older one untouched.
3. **A supersession target that changed after the assessment** used to quarantine the batch.
   It now raises `StaleLifecycleTarget`: nothing commits, and the next run assesses again
   against the new state.

Removed with the diversion: `_commit_quarantine` and its helpers, `CandidatesAlreadyQuarantined`,
the `compile-quarantine:` operation prefix, and the `quarantined` batch outcome.

## What still needs a person

Nothing in the ordinary flow. A doubtful claim is recorded, not queued. What a person may
still want is to look at doubtful claims on a page, the `quarantined` entries in its ledger.
Nothing requires it.

## Known limit

A claim with no ledger candidate, whose text the secondary search finds on a page without a
claim ledger, is still judged doubtful (`retrieval-only context has no verified claim ledger`).
It no longer blocks anything, but such claims are kept out of claim retrieval. Whether that
route should trust the claim instead is a separate decision.
