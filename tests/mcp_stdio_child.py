"""The MCP server as a child process, set up to do one thing that broke it.

`--stray-write`: its `vault_status` tool writes a line to fd 1 itself, as native
code does, before it answers.

`--loader-lock <trigger> <reached> <release> <released>`: what a MinGW runtime's
start-up did on 2026-09-30. Once `trigger` exists, a thread takes the Windows
loader lock, as `LoadLibraryExW` does, and queries fd 0 with `lseek`, as the
runtime does. It writes `reached` once that query returns, holds the lock until
`release` exists, and writes `released` after letting it go: a process that
exits holding it never finishes exiting.

A fixture process, not a test.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mcp_server  # noqa: E402


def _answer(*, deadline=None) -> dict:
    return {"answered": True}


def _answer_after_a_stray_write(*, deadline=None) -> dict:
    os.write(1, b"a stray line, as native code writes it\n")
    return _answer()


def _hold_loader_lock(
    trigger: Path, reached: Path, release: Path, released: Path
) -> None:
    ntdll = ctypes.WinDLL("ntdll")
    lock, unlock = ntdll.LdrLockLoaderLock, ntdll.LdrUnlockLoaderLock
    lock.argtypes = [
        ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_size_t)
    ]
    unlock.argtypes = [ctypes.c_ulong, ctypes.c_size_t]
    state, cookie = ctypes.c_ulong(), ctypes.c_size_t()
    while not trigger.exists():
        time.sleep(0.05)
    lock(0, ctypes.byref(state), ctypes.byref(cookie))
    try:
        try:
            os.lseek(0, 0, os.SEEK_CUR)
        except OSError:
            pass
        reached.write_text("fd 0 answered", encoding="utf-8")
        while not release.exists():
            time.sleep(0.05)
    finally:
        unlock(0, cookie.value)
    released.write_text("", encoding="utf-8")


def main() -> int:
    mcp_server._vault_status = _answer
    if sys.argv[1:2] == ["--stray-write"]:
        mcp_server._vault_status = _answer_after_a_stray_write
    if sys.argv[1:2] == ["--loader-lock"]:
        threading.Thread(
            target=_hold_loader_lock,
            args=tuple(Path(argument) for argument in sys.argv[2:6]),
            daemon=True,
        ).start()
    return mcp_server.run_server()


if __name__ == "__main__":
    sys.exit(main())
