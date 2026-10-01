"""The MCP server answers while another thread holds the Windows loader lock.

On 2026-09-30 a `log_decision` call never answered, past its own 10 s deadline:
a scipy DLL load held the loader lock while its runtime waited on fd 0, which had
the SDK's stdin read pending, and the event loop waited in `Thread.start()` for
that lock. `tests/mcp_stdio_child.py` replays the DLL's part without scipy. See
`docs/research/2026-10-01-the-mcp-pipes-leave-the-standard-descriptors.md`.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.slow_machine import SHORT_TIMEOUT

CHILD = Path(__file__).resolve().parent / "mcp_stdio_child.py"


class _Client:
    def __init__(self, process: subprocess.Popen, stdin) -> None:
        self.process = process
        self.stdin = stdin
        self.lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(b"")

    def send(self, message: dict) -> None:
        self.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.stdin.flush()

    def answer(self, identifier: int, timeout: float) -> dict:
        end = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                raise AssertionError(f"no answer to request {identifier}") from None
            assert line, "the server closed its output"
            message = json.loads(line)
            if message.get("id") == identifier:
                return message

    def initialize(self) -> None:
        self.send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "deadlock-test", "version": "0"},
            },
        })
        self.answer(1, SHORT_TIMEOUT)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def vault_status(self, identifier: int, timeout: float) -> dict:
        self.send({
            "jsonrpc": "2.0", "id": identifier, "method": "tools/call",
            "params": {"name": "vault_status", "arguments": {}},
        })
        return self.answer(identifier, timeout)


def _environment(vault: Path) -> dict:
    return dict(
        os.environ,
        LLM_WIKI_ROOT=str(vault),
        LLM_WIKI_STATE_ROOT=str(vault),
        LLMWIKI_NO_ENCODER_WARMUP="1",
        PYTHONUTF8="1",
    )


def _stop(process: subprocess.Popen, stdin) -> None:
    try:
        stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=SHORT_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_for(path: Path) -> bool:
    end = time.monotonic() + SHORT_TIMEOUT
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.05)
    return path.exists()


def _named_pipe_stdin():
    """The kind of pipe Node gives a child; a synchronous query on it waits behind a read."""
    import msvcrt
    from asyncio import windows_utils

    read_end, write_end = windows_utils.pipe(duplex=False, overlapped=(False, False))
    child_end = msvcrt.open_osfhandle(read_end, os.O_RDONLY)
    our_end = os.fdopen(msvcrt.open_osfhandle(write_end, 0), "wb", buffering=0)
    return child_end, our_end


def test_a_stray_print_does_not_reach_the_client(tmp_path):
    process = subprocess.Popen(
        [sys.executable, str(CHILD), "--stray-write"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_environment(tmp_path),
    )
    client = _Client(process, process.stdin)
    try:
        client.initialize()
        reply = client.vault_status(2, SHORT_TIMEOUT)
        assert '"answered":true' in reply["result"]["content"][0]["text"]
    finally:
        _stop(process, process.stdin)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows loader lock")
def test_the_server_answers_while_the_loader_lock_is_held(tmp_path):
    trigger, reached, release, released = (
        tmp_path / name for name in ("trigger", "reached", "release", "released")
    )
    child_end, our_end = _named_pipe_stdin()
    try:
        process = subprocess.Popen(
            [sys.executable, str(CHILD), "--loader-lock",
             str(trigger), str(reached), str(release), str(released)],
            stdin=child_end, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_environment(tmp_path),
        )
    finally:
        os.close(child_end)
    client = _Client(process, our_end)
    try:
        client.initialize()
        client.vault_status(2, SHORT_TIMEOUT)

        trigger.write_text("", encoding="utf-8")
        assert _wait_for(reached), "a query on fd 0 waited behind the server's read"

        reply = client.vault_status(3, SHORT_TIMEOUT)
        assert '"answered":true' in reply["result"]["content"][0]["text"]
    finally:
        release.write_text("", encoding="utf-8")
        if trigger.exists():
            _wait_for(released)
        _stop(process, our_end)
