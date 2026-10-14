# The owner can acknowledge a refused attempt

A refused attempt is a transaction the writer quarantined: a precondition moved under it, or the publication guard blocked its content. Doctor counts one as an `error` until it can prove the work happened anyway: a retry in its lineage committed, everything it meant to create was created, or the day a refused compile read is compiled now. Some refused attempts can never be proved that way. A tool breadcrumb that lost its race is gone, and a compile retried after a fix may publish its notes under other slugs. Each such attempt kept doctor red for good, with no command to clear it: discard refuses a quarantined transaction and prune keeps it. On 2026-10-14 the vault carried 88 of them, all from causes already fixed, and a check that is always red stops being read. So the owner may now acknowledge one: `doctor.py --acknowledge-refused <id>... --reason <text>` (or `open` for every attempt open at that moment). The acknowledgement is recorded in `run/acknowledged-refusals.json` with the time and the reason. Doctor then reports the attempt as acknowledged, not as an error.

## Consequences

- Nothing is deleted. The transaction row, its artifacts and its `quarantined` state stay. A quarantined transaction still blocks deleting `run/`; the runtime deletion contract does not change.
- Only an attempt that is quarantined and still unresolved can be acknowledged. An attempt refused later is a new finding until it is acknowledged too; `open` covers only the attempts open when it runs.
- The file is one owner-only runtime file, replaced only when its bytes still match what was read. It is operational state, like the rest of `run/`. Deleting it brings the findings back; it hides no data.
- The reason is redacted before it is stored, like every other runtime record.

## Considered Options

- Delete the rows from the transaction database: rejected. It leaves the artifacts in `run/transactions/` with no owner, which doctor reports as corrupt, and it destroys the evidence of a loss.
- Stop counting a refused attempt after some age: rejected. Time does not show that anything was recovered, and a real loss would go green with no one deciding it.
- Add a new state to the transaction schema: rejected. It changes a versioned database the writers share, for a fact only doctor reads.
