# A backlog drains in full batches

Dated 2026-09-28. Files: `scripts/integration_adapter.py`,
`tests/test_a_backlog_drains_in_full_batches.py`.

## What was seen

One Codex session in one registered repository worked on and off for about 27 hours,
from 2026-09-26 23:38 to 2026-09-28 02:14 local. It made about 4 800 tool calls and
1 100 file changes. By the morning of 09-28 its project had 5 964 pending checkpoint
events: 11.4 MB inside a 15.7 MB `run/state.json`. `doctor` refused the file, because it
is over its 4 MiB bound. Fourteen captures were lost. The nightly failed because its
reclaim step was killed at its 180 s limit.

Measured on the live vault:

- A no-op `update_state` took 1.8 s at 15 MB.
- A hand-run drain with no competing writer moved 90 events in 132 s. Sampled with
  `py-spy`, it spent its time in `json` encoding and `save_state`, then timed out on the
  writer gate when a compile started.
- The simulated plan for that queue selected 5 events per batch, ending at each file
  change.

## Why

A drain cycle claims a window, records the batch as in flight, writes the journal and
commits. That is four full rewrites of `run/state.json`. A batch ended at the first
checkpoint decision, and in a working session a file change comes every five events or
so. So draining cost about one rewrite per event, and each rewrite cost more as the
queue grew.

A `PostToolUse` hook has about 3.5 s of budget
(`docs/research/2026-09-26-a-hook-never-starts-a-write-it-cannot-finish.md`). Once the
file passed a few megabytes, the hook spent that budget enqueuing its own event and
never started a drain. The queue then only grew. The drain had first fallen behind on
09-26, when the compile and the hooks were fighting over the writer gate
(`docs/research/2026-09-27-a-busy-gate-does-not-fail-the-compile.md`).

## The fix

A claimed window that holds at least `MAX_PENDING_CHECKPOINT_ITEMS` events is past its
debounce by definition. It now drains as one batch of up to
`_bounded_pending_batch_count` events, which is at most 100 evidence ids and the per-list
bounds. This is the same batch `_batch_plan` already flushes as `batch_flush` when a
checkpoint would overflow. Every command, changed file and blocker in the batch is
merged into the journal entry as before. The intermediate `current_task` values are kept
in `current_task_operations`, and the last one is the current task. A short queue still
checkpoints at its first decision, exactly as before.

On the queue above, that is about 20 times fewer rewrites per event.

## Not changed

- The queue still lives in `run/state.json`, so a hook still pays for the whole file on
  every enqueue. Moving it into the runtime SQLite databases would make that cost
  independent of the backlog. It changes a runtime contract, so it needs its own decision.
- Six single-event queues of directories that are no longer registered repositories
  still fail every drain as `Unregistered`. They expire after 30 days
  (`PENDING_EVENT_MAX_AGE`).
