# The MCP pipes leave the standard descriptors

Dated 2026-10-01. Files: `scripts/mcp_stdio.py`, `scripts/mcp_server.py`,
`scripts/markdown_transaction.py`, `tests/mcp_stdio_child.py`,
`tests/test_mcp_stdio_deadlock.py`.

## What was seen

On Windows, an MCP call to the server sometimes never answered, not even with the
10 s timeout envelope. On 2026-09-30 a `log_decision` call hung until the client gave
up after 1800 s; the same happened on 2026-09-27 and 2026-09-28. Nothing reached the
daily log. A `recall` earlier in the same session had answered with
`optional_stage_timeout`, leaving its dense-retrieval thread still importing.

`py-spy` stacks of the stuck server showed three threads in a cycle:

1. The SDK's stdin reader (an anyio worker thread) in `ReadFile` on fd 0, waiting for
   the client.
2. `llm-wiki-optional-retrieval`, importing `sentence_transformers → sklearn → scipy`,
   inside `LoadLibraryExW` for a scipy `.pyd`. The vault's scipy is a MinGW build; the
   start-up code of its gfortran runtime queries fds 0–2. That thread holds the Windows
   loader lock while it waits.
3. The event loop, in `Thread.start()`, waiting for the loader lock: the call's worker
   thread is started per call, and anyio starts a new worker for the stdout write once
   its idle workers have expired (10 s).

The client waits for the answer, the loop waits for the loader lock, the import waits
on fd 0, and fd 0 waits for the client.

## Why a query on fd 0 waits

Reproduced in child processes on this machine:

- The client's pipe matters. Node, and Git Bash, give the child a **named pipe**. A
  synchronous operation on a synchronous named-pipe handle waits behind a read pending
  on the same file object. `os.lseek(0, …)`, `PeekNamedPipe` and the CRT's `_fstat64`
  on fd 0 all hung while another thread read fd 0; with an anonymous pipe
  (`subprocess.PIPE`) none of them did. That is why ordinary subprocess tests never saw
  it.
- With a reader on fd 0, `import scipy.linalg` from the vault's `.venv` did not finish
  in 40 s. With the pipe moved to a private descriptor and fd 0 pointed at `NUL`, it
  took 0.8 s.
- End to end, an unchanged server with a background `import scipy.interpolate,
  sklearn.linear_model` and 12 s of client silence: the import had not finished. The
  changed server: it finished in 4.5 s.

## The fix

- **The pipes leave fds 0 and 1.** `mcp_stdio.isolated_stdio_server` duplicates the
  client's pipes onto private descriptors and points fds 0 and 1 at the null device for
  the server's life. A library that queries the standard descriptors, or a child process
  that inherits them (`icacls`, for one), sees `NUL`. Native code that writes to fd 1 no
  longer corrupts the protocol. The descriptors are restored when the transport exits.
- **The event loop never starts a thread.** One reader thread and one writer thread
  serve the pipes for the server's whole life, and the `MCP_WORKER_SLOTS` workers are
  started once, before the encoder warm-up, instead of one per call. A DLL load that
  holds the loader lock can still delay the work inside a call, but no longer the
  deadline's answer.
- `icacls` runs with `stdin=DEVNULL`.

## How it is tested

`tests/mcp_stdio_child.py` runs the server with a thread that does what the runtime did:
it takes the loader lock with `LdrLockLoaderLock` and calls `lseek` on fd 0. The test
gives the server a named pipe, as Node does. On the unchanged code each of its three
checks fails: the `lseek` waits behind the read, the call made while the lock is held
gets no answer, and a line written to fd 1 reaches the client. No scipy is needed.

## Not changed

`markdown_transaction._validate_adoption_with_retry` still retries for up to 30 s on its
own clock. The caller's deadline answers on time regardless; the abandoned worker just
holds its slot longer.
