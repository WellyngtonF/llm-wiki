# A queue with no project is discarded

Dated 2026-09-28. Files: `scripts/integration_adapter.py`,
`scripts/reclaim_runtime_state.py`, `tests/test_a_queue_with_no_project_is_discarded.py`.

## What was seen

On 2026-09-28 six checkpoint queues had sat in `run/state.json` since 2026-09-24, one event
each. Every nightly reported them as `6 project(s) failed`. Each was named after a scratch
folder inside a registered repository: a sandbox payload directory, a probe, a stubs folder
and similar. A hand-run backlog drain answered the same thing for all six:
`Unregistered: work state '<name>' belongs to no registered repository`.

## Why

The old code enqueued them on 09-24, when every working directory was its own project.
Registered projects arrived on 09-25 (ADR 0002). Since then an unregistered directory
enqueues nothing: `_enqueue_project_checkpoint` returns before writing.

The projects migration run on 09-25 has a step that clears queues no registered repository
owns (`_clear_stale_keys`). Run again on 09-28 against the vault, that step's own rule
(`_is_live_key`) classifies all six as stale. So they should have gone that day. No record
of that migration's result survives. One plausible cause is that `_pending_keys` treats an
unreadable `state.json` as "no keys" without saying so. That is not confirmed.

What kept them is the drain. It met `Unregistered`, recorded a failure, and left the queue
for the next pass. The only exit was the 30-day expiry (`PENDING_EVENT_MAX_AGE`).

## The fix

When the unattended backlog drain meets `Unregistered`, it discards the queue and its
in-flight record, provided the queue has been quiet for `UNREGISTERED_QUEUE_GRACE` (3 days).
Quiet is measured against the newest pending event, as expiry is. The grace is there
because a registered repository whose map entry is briefly unusable also answers
`Unregistered`, and its events must survive that. The drain returns the discards, and the
reclaim summary in the nightly log names how many there were. They are not written to the
capture-failure trail: that trail counts a failure as a lost capture, and this is the rule
working, not a loss. The session record still keeps what the work was.

## Not changed

- The hook path already enqueues nothing for unregistered work.
- `migrate_projects._pending_keys` still returns an empty set on a read failure. The drain
  now covers the result, so fixing the migration's silence is left for a change to the
  migration itself.
