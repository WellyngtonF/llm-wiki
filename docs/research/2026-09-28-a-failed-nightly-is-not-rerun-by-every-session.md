# A failed nightly is not rerun by every session

Dated 2026-09-28. Files: `scripts/session_start_context.py`,
`tests/test_a_failed_nightly_is_not_rerun_by_every_session.py`.

## What was seen

The owner reported that the machine had become slow to start and to shut down. On
2026-09-26 the nightly ran four times: at 21:00 from the scheduler, then at 22:02, 23:00
and 23:55. The last run ended at 00:01, a few minutes before shutdown. On 09-27 another
full pass started at 10:00, one minute after the owner opened the agent host at login.
It ran next to a compile and three resumed sessions, and the hooks logged dozens of
`StateLockTimeout` errors in the first two minutes.

## Why

`_claim_nightly_catchup` skipped only when `last_nightly_date` had reached the due
evening, and only a success writes that date. Every pass in that stretch failed (see
`docs/research/2026-09-27-a-busy-gate-does-not-fail-the-compile.md`). So every session
start after the claim's 30-minute lease expired saw the evening as missed and started the
whole nightly again: compile, model calls, lint and generation refresh.

## The fix

A pass that ran and failed for the due evening counts as tried, just like one that
succeeded. The failure record carries the pass's own date, and a catch-up run the next
morning is dated after the evening it stood in for, so any date from the due evening on
counts. The next scheduled evening retries. A failure older than the due evening is
still caught up, and so is a machine that was off at 21:00.

## Not changed

`doctor` and the session-start health block still report the failure, so a failing
nightly stays visible.
