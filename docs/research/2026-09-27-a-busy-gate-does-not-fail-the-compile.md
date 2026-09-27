# A busy gate does not fail the compile

Dated 2026-09-27. Files: `scripts/compile_memory.py`,
`tests/test_a_busy_gate_does_not_fail_the_compile.py`.

## What was seen

The nightly failed on 2026-09-26 and 2026-09-27, and failed again every time a session
started a catch-up pass. Four of its five compile failures were the writer gate:

- `external LLM work is forbidden during persisted writer ownership` (twice);
- `timed out waiting for the global Markdown writer gate` (twice). The one on 09-27
  planned 5 of 11 batches over 16 minutes, then lost everything left.

Measured on the live vault 2026-09-27, sampling `writer_owners` every 0.1 s for 150 s
with three agent sessions working: the gate was held 25% of the time, by 35 holders.
Hook captures held it for under 0.1 s each. Three holders held it for a long time: an
unidentified process for 29.9 s, the compile itself for over 15 s during publication,
and an agent's hand-run script. The research of 2026-09-26 measured project
checkpoints holding it for 27, 31 and 42 s.

## Why

Two waits in the compile were sized for a hook, not for unattended work:

- Before each model call, `_assert_external_work_allowed` waited up to 10 s for *any*
  process to release the gate, then refused. The invariant it protects covers only this
  thread: never call a model while holding the gate, through any coordinator. Another
  process's write does not conflict with a model call. The call takes no lock, and the
  publication after it takes the gate on its own.
- Publication entered the gate with the default wait, `markdown_busy_ms` (10 s). That
  bound is right for a hook, because a person is waiting on it. It is wrong for a
  compile that has already spent minutes planning.

So any single holder over 10 s during a compile's lifetime was enough to fail it.

## The fix

- A model call is refused only when this thread owns the gate. That still covers the
  persisted row and any coordinator. When another process holds the gate, the call goes
  ahead at once.
- Publication waits up to `COMPILE_PUBLICATION_GATE_SECONDS` (120 s) for the gate,
  bounded by the compile's own deadline. That is well above the longest hold measured
  (42 s).

## Not changed

- Hooks keep their short waits (`docs/research/2026-09-26-a-hook-never-starts-a-write-it-cannot-finish.md`).
- The compile still holds the gate for many seconds while it publishes: the claim-index
  rebuild runs `icacls` several times per file under the gate. Shortening that hold
  would help every hook behind it. It is a separate change.
- A session-started catch-up pass still reruns the whole nightly whenever the last one
  failed, every 30 minutes. That multiplied this failure into slow logins and
  shutdowns. It is a separate change.
