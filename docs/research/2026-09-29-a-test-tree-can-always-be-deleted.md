# A test tree can always be deleted

Dated 2026-09-29. Files: `tests/deletable_trees.py`, `tests/conftest.py`,
`pyproject.toml`, `tests/test_a_test_tree_can_always_be_deleted.py`.

## What was seen

The owner reported that the machine took a very long time to start and shut down. The
User Profile Service's logon notification (Microsoft-Windows-User Profile Service/Operational,
events 1 → 2) took 10–30 s on every logon until 2026-09-23. From 2026-09-25 it took
4–6.5 minutes, and on 2026-09-29 it took 17 minutes twice in a row. No user process runs
during that window, and nothing of llm-wiki ran in it.

A Process Monitor boot trace of the 17-minute logon, 8.6 million events in four minutes,
sampled one in a hundred:

- 5.75 M of them came from `svchost.exe -k netsvcs -p -s ProfSvc`.
- 5.67 M of those were under `%LOCALAPPDATA%\Temp`: `CreateFile`, `QueryDirectory` and
  `SetStorageReservedIdInformation`. At logon the service tags every file in `%TEMP%` for
  Windows 10 reserved storage, so the logon grows with the number of files there.
- 4.96 M were under `Temp\pytest-of-welly`, in `garbage-*` directories.

`pytest-of-welly` held 49 `garbage-*` directories, about 900 000 files. Until 2026-09-23
they held 25–111 files each. From 2026-09-23 20:06 on, each full-suite run left 61 000–65 000
files, and about 15 such runs happened between 09-23 and 09-27.

## Why

pytest renames an old temporary tree to `garbage-*` when it cannot delete it, and tries
again later. Its error handler clears the read-only *attribute*. It does not touch an ACL.
The product narrows ACLs on purpose. `archive_daily` makes a BagIt package immutable with
`icacls /inheritance:r /grant:r <owner>:(OI)(CI)(RX)` (files get `(R)`). The caches, `run/`
and managed language-server copies get owner-only ACLs. `Remove-Item` on one such tree
answered `Access denied`. So every test that archived, or hardened a cache, left its tree
behind for good. The session state root's `shutil.rmtree(ignore_errors=True)` failed the
same way, silently: 50 `llm-wiki-test-state-*` directories were left in `%TEMP%`.

## The fix

- `release_test_tree` runs `icacls <tree> /reset /T /C /Q`. The owner always keeps
  `WRITE_DAC`, so every ACL returns to the inherited one and the tree becomes deletable.
- At session end, before pytest's own cleanup (`tryfirst`), the pytest root
  (`pytest-of-<user>`, or the explicit `--basetemp`) is released. pytest's exit-time
  cleanup can then delete old sessions and their `garbage-*` trees. The session state root
  is released before its `rmtree`.
- `tmp_path_retention_policy = "failed"`: a passed test's tree no longer stays in `%TEMP%`.
  pytest keeps only the trees of failed tests, for inspection.

## Not changed

The product still narrows ACLs; that is its security contract. Only the tests give them
back.
