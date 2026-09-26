# A hook never starts a write it cannot finish

Dated 2026-09-26. Files: `scripts/integration_adapter.py`, `scripts/codex_memory.py`,
`tests/test_a_hook_never_starts_a_write_it_cannot_finish.py`.

## What was seen

With three agent sessions working, the compile failed twice in two new ways:
`external LLM work is forbidden during persisted writer ownership` (it waits at most
10 s for the global writer gate to be free before a model call) and
`timed out waiting for the global Markdown writer gate`. Tool breadcrumbs were dropped
with the same timeout (`logs/capture-failures.jsonl`).

Measured on the live vault:

- Sampling `writer_owners` every 0.1 s for two minutes: the global gate was free 14% of
  the time. Three holders were project checkpoints of one repository, held 27 s, 31 s and
  42 s.
- The transaction table since 12:00: committed project checkpoints took median 0.5–0.6 s,
  longest 1.74 s. Fifteen were `quarantined`, each lasting 30–71 s: the writer was gone
  and its transaction was recovered after its lease lapsed.
- Orphan `.journal.md.*.tmp` and `.state.md.*.tmp` files beside the journals: writers
  that stopped between the temporary write and the rename.

## Why

Every hook except session start drained its repository's checkpoint queue first, waiting
for the gate for `markdown_busy_ms` (10 s). The shipped `PostToolUse` and
`UserPromptSubmit` hooks get 5 s, and "Claude Code cancels a `command` hook that reaches
its `timeout`" ([hooks reference](https://code.claude.com/docs/en/hooks)). A hook that got
the gate late started its write and was cancelled inside it. The gate stayed held until
the 30 s lease lapsed, because a dead owner is reclaimed only after its lease lapses.
Every hook behind it waited, was cancelled, and sometimes wedged the gate again. That is
the cascade the samples show.

`docs/research/2026-09-17-every-hook-writer-gives-up-before-its-host-does.md` fixed this for
the daily-log breadcrumb (3 s inside the 5 s host). The checkpoint ran before the
breadcrumb, inside the same 5 s, with no bound of its own.

## The fix

- A hook has a deadline: the host timeout for its event (`HOOK_HOST_SECONDS`, the smaller
  of the two shipped files) minus 1.5 s for `uv` to start the interpreter and for the exit.
- In a hook the checkpoint event is still enqueued first. The enqueue is durable and quick,
  so a cancelled hook loses no event. The capture runs next, so the breadcrumb gets the
  budget its own research gave it. The queue is drained last.
- The drain starts a write only if the write can finish: its gate wait ends at the
  deadline minus 2 s (`CHECKPOINT_WRITE_SECONDS`, above the 1.74 s measured). Past that
  point it writes nothing. What stays pending is written by the next hook of any session
  in the repository, or by the nightly backlog drain.
- Session start keeps the checkpoint first, because the context it prints reads that
  state. It already waits only 0.25 s. Library callers without a deadline behave as before.
- A test reads both shipped hook files and fails if a budget no longer fits the host.

## Not changed

A dead writer's gate is still reclaimed only after its lease lapses, even when the process
is provably gone. Reclaiming at once would shorten any wedge from 30 s to the next
attempt. It changes the ownership registry's proof rule, which every actor shares, so
it needs its own decision.
