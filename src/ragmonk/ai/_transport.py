"""Reusable, bounded stdio JSON transport for subscription runtimes
(subscription plan, Phase 2).

Both the Codex adapter (Phase 2) and the Copilot adapter (Phase 3) talk to
a local child process over newline-delimited JSON on stdio. This module
owns the parts that are identical between them and easy to get subtly
wrong: newline framing that reassembles a message split across reads,
request/response correlation by id, tolerance of unknown-id responses and
of notifications, and mapping malformed output to a typed
``AiInvalidResponseError`` rather than letting a caller mistake it for an
answer.

Nothing here is provider-specific: ``JsonRpcClient`` speaks over an
abstract ``ByteStream`` so the whole request/response/framing contract can
be exercised against a scripted in-memory stream (release gate 4: split
JSON events, stderr noise, wrong request ids, oversized output, ...)
without spawning a real process, while the real ``StdioByteStream`` wires
the same client to an actual subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, Protocol

from ragmonk.ai.base import AiInvalidResponseError, AiProviderError, AiTimeoutError

# Hard ceiling on a single unframed message. A runaway or hostile runtime
# emitting an unbounded line without a newline must not exhaust memory --
# past this the transport fails the read as an invalid response.
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class ByteStream(Protocol):
    """A bidirectional byte channel. ``read`` returns the next available
    chunk (``b""`` at EOF); framing into messages is the client's job, so
    a chunk may contain part of a message, several messages, or a split
    across a boundary.
    """

    async def read(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...

    async def aclose(self) -> None: ...


class JsonRpcError(AiProviderError):
    """An error *response* from the runtime (a well-formed message whose
    ``error`` field is set). Carries the runtime's own ``code`` so the
    provider adapter can translate it into a semantic error (quota,
    expired login, ...); left as a generic provider failure otherwise.
    """

    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"runtime error {code}: {message}")
        self.code = code
        self.runtime_message = message


class JsonRpcClient:
    """Minimal id-correlated JSON message client over a ``ByteStream``.

    Messages are single-line JSON objects. A response carries the ``id``
    of the request it answers plus either ``result`` or ``error``; a
    message with no ``id`` is a notification and is ignored; a response
    whose ``id`` matches no in-flight request is ignored (a late/duplicate
    reply must never crash the client or resolve the wrong call).
    """

    def __init__(self, stream: ByteStream) -> None:
        self._stream = stream
        self._buffer = b""
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._fatal: Exception | None = None

    async def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                chunk = await self._stream.read()
                if not chunk:
                    self._fail_all(
                        AiInvalidResponseError("ai runtime closed the connection unexpectedly")
                    )
                    return
                self._buffer += chunk
                if len(self._buffer) > _MAX_MESSAGE_BYTES and b"\n" not in self._buffer:
                    self._fail_all(
                        AiInvalidResponseError(
                            "ai runtime sent an oversized message with no framing"
                        )
                    )
                    return
                while b"\n" in self._buffer:
                    raw, self._buffer = self._buffer.split(b"\n", 1)
                    line = raw.strip()
                    if line:
                        self._handle_line(line)
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise
        except Exception as exc:  # noqa: BLE001 - surface any stream fault to callers
            self._fail_all(AiProviderError(f"ai runtime transport failed: {exc}"))

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A malformed line on the response channel means the protocol
            # is broken -- fail every waiting caller rather than silently
            # dropping it and letting them hang to their deadline.
            self._fail_all(AiInvalidResponseError(f"ai runtime sent malformed JSON: {exc}"))
            return
        if not isinstance(message, dict):
            self._fail_all(AiInvalidResponseError("ai runtime sent a non-object message"))
            return
        if "id" not in message or message["id"] is None:
            return  # notification: nothing here consumes them yet
        msg_id = message["id"]
        future = self._pending.pop(msg_id, None) if isinstance(msg_id, int) else None
        if future is None or future.done():
            return  # unknown / late / duplicate id -- tolerated, not fatal
        if "error" in message and message["error"] is not None:
            err = message["error"]
            if isinstance(err, dict):
                future.set_exception(
                    JsonRpcError(err.get("code"), str(err.get("message", "unknown error")))
                )
            else:
                future.set_exception(JsonRpcError(None, str(err)))
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {"result": result})

    def _fail_all(self, exc: Exception) -> None:
        self._fatal = exc
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float
    ) -> dict[str, Any]:
        if self._closed:
            raise AiProviderError("ai runtime client is closed")
        if self._fatal is not None:
            raise self._fatal
        await self.start()
        self._next_id += 1
        msg_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = future
        payload = {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        await self._stream.write(json.dumps(payload).encode("utf-8") + b"\n")
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise AiTimeoutError(
                f"ai runtime did not answer {method!r} within {timeout:.0f}s"
            ) from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        await self._stream.write(json.dumps(payload).encode("utf-8") + b"\n")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            # Best-effort shutdown: a cancelled reader raises CancelledError,
            # and any late stream fault is irrelevant once we are closing.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._fail_all(AiProviderError("ai runtime client is closed"))
        await self._stream.aclose()


class StdioByteStream:
    """A ``ByteStream`` over an ``asyncio`` subprocess: reads the child's
    stdout in chunks and drains its stderr on a side task so noisy
    diagnostics can never block or corrupt the JSON channel. Closing
    terminates and reaps the child on every exit path (release gate 8).
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._stderr_task: asyncio.Task[None] | None = None
        if process.stderr is not None:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(process.stderr))

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        try:
            while await stderr.read(65536):
                pass
        except Exception:  # noqa: BLE001 - stderr draining is best-effort
            return

    async def read(self) -> bytes:
        assert self._process.stdout is not None
        return await self._process.stdout.read(65536)

    async def write(self, data: bytes) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def aclose(self) -> None:
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):  # pragma: no cover - already gone
                self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:  # pragma: no cover - stubborn child
                self._process.kill()
                await self._process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
