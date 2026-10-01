"""The MCP stdio transport: the client's pipes on private descriptors, served by
threads that start before anything else in the server runs.

On 2026-09-30 a `log_decision` call never answered, not even with the 10 s
timeout envelope; the client gave up after 1800 s. Three threads were stuck in a
cycle. The SDK's stdin reader had a synchronous read pending on fd 0, a named
pipe. A retrieval thread was importing scipy; the start-up code of its MinGW
runtime queries fd 0, and an operation on a synchronous pipe waits behind the
pending read, while that thread holds the Windows loader lock. The event loop
then started a thread, for the call's worker or for anyio's stdout writer, and
`Thread.start()` waits for the loader lock. The client sent nothing more, since
it was waiting for the answer, so the read never completed.

So fds 0 and 1 name the null device while the server runs, and the pipes live on
private descriptors that no library inspects and no child process inherits. One
reader thread and one writer thread serve them for the server's whole life, so
the event loop never starts a thread to read or write a message. See
`docs/research/2026-10-01-the-mcp-pipes-leave-the-standard-descriptors.md`.
"""
from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp.shared.message import SessionMessage

_READ_BYTES = 64 * 1024


class StandardStreams:
    """The client's pipes, moved off fds 0 and 1, which name the null device instead."""

    def __init__(self) -> None:
        sys.stdout.flush()
        self.input = os.dup(0)
        self.output = os.dup(1)
        self._saved = (os.dup(0), os.dup(1))
        null = os.open(os.devnull, os.O_RDWR)
        try:
            os.dup2(null, 0)
            os.dup2(null, 1)
        finally:
            os.close(null)

    def restore(self) -> None:
        sys.stdout.flush()
        for standard, saved in zip((0, 1), self._saved):
            os.dup2(saved, standard)
            os.close(saved)


def _read_lines(descriptor: int, deliver) -> None:
    pending = b""
    try:
        while True:
            try:
                chunk = os.read(descriptor, _READ_BYTES)
            except OSError:
                break
            if not chunk:
                break
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                if not deliver(line + b"\n"):
                    return
        if pending:
            deliver(pending)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, jobs: queue.SimpleQueue) -> None:
    try:
        while (job := jobs.get()) is not None:
            data, written = job
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view):]
            except OSError as error:
                written(error)
            else:
                written(None)
    finally:
        os.close(descriptor)


def _message_from(line: bytes) -> SessionMessage | Exception:
    try:
        message = types.JSONRPCMessage.model_validate_json(
            line.decode("utf-8", errors="replace")
        )
    except Exception as error:
        return error
    return SessionMessage(message)


def _settle(written: asyncio.Future, error: OSError | None) -> None:
    if written.done():
        return
    if error is None:
        written.set_result(None)
    else:
        written.set_exception(error)


def _notifier(loop: asyncio.AbstractEventLoop, written: asyncio.Future):
    def notify(error: OSError | None) -> None:
        try:
            loop.call_soon_threadsafe(_settle, written, error)
        except RuntimeError:
            pass

    return notify


@asynccontextmanager
async def isolated_stdio_server():
    """`mcp.server.stdio.stdio_server`, without a thread started per message."""
    loop = asyncio.get_running_loop()
    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)
    jobs: queue.SimpleQueue = queue.SimpleQueue()

    def deliver(line: bytes) -> bool:
        item = _message_from(line)
        try:
            asyncio.run_coroutine_threadsafe(
                read_stream_writer.send(item), loop
            ).result()
        except Exception:
            return False
        return True

    def reader(descriptor: int) -> None:
        try:
            _read_lines(descriptor, deliver)
        finally:
            try:
                asyncio.run_coroutine_threadsafe(
                    read_stream_writer.aclose(), loop
                ).result()
            except Exception:
                pass

    async def stdout_writer() -> None:
        async with write_stream_reader:
            async for session_message in write_stream_reader:
                json = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                )
                written = loop.create_future()
                jobs.put(((json + "\n").encode("utf-8"), _notifier(loop, written)))
                await written

    streams = StandardStreams()
    try:
        threading.Thread(
            target=reader, args=(streams.input,), name="llm-wiki-mcp-stdin",
            daemon=True,
        ).start()
        threading.Thread(
            target=_write_all, args=(streams.output, jobs),
            name="llm-wiki-mcp-stdout", daemon=True,
        ).start()
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(stdout_writer)
            try:
                yield read_stream, write_stream
            finally:
                write_stream.close()
    finally:
        jobs.put(None)
        streams.restore()
