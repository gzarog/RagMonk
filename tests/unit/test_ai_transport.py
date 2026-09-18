"""``ai/_transport.py`` JSON message client contract (subscription plan,
Phase 2), exercised entirely against a scripted in-memory byte stream --
no subprocess. Covers the failure modes release gate 4 requires: JSON
split across reads, several messages in one read, notifications and
unknown/late ids tolerated, error responses, malformed output, oversized
output, EOF, and per-request timeout.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest

from ragmonk.ai import _transport
from ragmonk.ai._transport import JsonRpcClient, JsonRpcError
from ragmonk.ai.base import AiInvalidResponseError, AiTimeoutError


class ChunkStream:
    """Feeds ``chunks`` (server->client bytes) in order, one per ``read``;
    records what the client writes. ``chunks`` is produced from each write
    by ``responder`` so request/response ordering is realistic.
    """

    def __init__(self, responder: Callable[[dict], list[bytes]]) -> None:
        self._responder = responder
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []

    async def read(self) -> bytes:
        return await self._queue.get()

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            message = {}
        for chunk in self._responder(message):
            self._queue.put_nowait(chunk)

    async def aclose(self) -> None:
        return None


def _line(**payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8") + b"\n"


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_request_returns_matching_result() -> None:
    def responder(msg: dict) -> list[bytes]:
        return [_line(jsonrpc="2.0", id=msg["id"], result={"ok": True})]

    async def scenario() -> dict:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            return await client.request("ping", timeout=5.0)
        finally:
            await client.aclose()

    assert _run(scenario()) == {"ok": True}


def test_response_split_across_chunks_is_reassembled() -> None:
    def responder(msg: dict) -> list[bytes]:
        full = _line(jsonrpc="2.0", id=msg["id"], result={"v": 1})
        return [full[:5], full[5:]]  # split mid-message

    async def scenario() -> dict:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            return await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    assert _run(scenario()) == {"v": 1}


def test_notification_and_unknown_id_are_ignored_before_real_response() -> None:
    def responder(msg: dict) -> list[bytes]:
        return [
            _line(jsonrpc="2.0", method="progress", params={"pct": 10}),  # notification
            _line(jsonrpc="2.0", id=999, result={"stale": True}),  # unknown id
            _line(jsonrpc="2.0", id=msg["id"], result={"real": True}),
        ]

    async def scenario() -> dict:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            return await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    assert _run(scenario()) == {"real": True}


def test_error_response_raises_jsonrpc_error_with_code() -> None:
    def responder(msg: dict) -> list[bytes]:
        return [_line(jsonrpc="2.0", id=msg["id"], error={"code": "quota", "message": "no more"})]

    async def scenario() -> None:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    with pytest.raises(JsonRpcError) as excinfo:
        _run(scenario())
    assert excinfo.value.code == "quota"


def test_malformed_json_is_invalid_response() -> None:
    def responder(msg: dict) -> list[bytes]:
        return [b"{ this is not json\n"]

    async def scenario() -> None:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    with pytest.raises(AiInvalidResponseError):
        _run(scenario())


def test_eof_fails_pending_request() -> None:
    def responder(msg: dict) -> list[bytes]:
        return [b""]  # EOF

    async def scenario() -> None:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    with pytest.raises(AiInvalidResponseError):
        _run(scenario())


def test_no_response_times_out() -> None:
    def responder(msg: dict) -> list[bytes]:
        return []  # server never answers

    async def scenario() -> None:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            await client.request("m", timeout=0.2)
        finally:
            await client.aclose()

    with pytest.raises(AiTimeoutError):
        _run(scenario())


def test_oversized_unframed_message_is_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_transport, "_MAX_MESSAGE_BYTES", 64)

    def responder(msg: dict) -> list[bytes]:
        return [b"x" * 128]  # 128 bytes, no newline -> exceeds the 64B cap

    async def scenario() -> None:
        client = JsonRpcClient(ChunkStream(responder))
        try:
            await client.request("m", timeout=5.0)
        finally:
            await client.aclose()

    with pytest.raises(AiInvalidResponseError):
        _run(scenario())
