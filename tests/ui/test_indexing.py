"""Indexing page, start, failed-file inspection, live-progress stream (§6)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from tests.ui.conftest import csrf_headers

from ragmonk.service.index_service import INDEXER


def test_indexing_page_renders(indexed_client: TestClient) -> None:
    response = indexed_client.get("/indexing")
    assert response.status_code == 200
    assert "Queue depth" in response.text


def test_failed_files_page(indexed_client: TestClient) -> None:
    response = indexed_client.get("/indexing/failed")
    assert response.status_code == 200


def test_start_indexing_runs_in_background(indexed_client: TestClient) -> None:
    headers = csrf_headers(indexed_client)
    response = indexed_client.post("/indexing/start", headers=headers)
    assert response.status_code == 204
    # Wait for the background run to settle so it doesn't leak into other
    # tests sharing the process-wide INDEXER.
    for _ in range(100):
        if not INDEXER.is_running:
            break
        time.sleep(0.1)
    assert not INDEXER.is_running
    snapshot = INDEXER.snapshot()
    assert snapshot["last_summary"] is not None


def test_sse_stream_is_event_stream() -> None:
    # Exercise the stream generator directly with a request that reports
    # disconnected immediately -- driving it through TestClient would leave
    # the infinite keepalive loop open and block the test client's portal
    # on close. This still proves the media type and that the loop honors
    # disconnect (terminates rather than running forever).
    import asyncio

    from ragmonk.ui.events import indexing_event_stream

    class _StubRequest:
        async def is_disconnected(self) -> bool:
            return True

    async def _drive() -> None:
        response = await indexing_event_stream(_StubRequest())  # type: ignore[arg-type]
        assert response.media_type == "text/event-stream"
        chunks = [chunk async for chunk in response.body_iterator]
        # Disconnected immediately -> the generator exits without emitting.
        assert chunks == []

    asyncio.run(_drive())
