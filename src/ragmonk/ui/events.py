"""Server-Sent Events for live indexing progress.

Admin UI plan, Phase 3 (§6.5): stream indexing progress to the browser
over SSE so HTMX can update the page without a heavy frontend framework.
The event source is the in-memory :data:`ragmonk.service.index_service.INDEXER`
event log; this module just formats those events as an SSE response.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from starlette.requests import Request
from starlette.responses import StreamingResponse

from ragmonk.service.index_service import INDEXER

_POLL_INTERVAL_SECONDS = 0.5
_IDLE_KEEPALIVE_SECONDS = 15.0


async def indexing_event_stream(request: Request) -> StreamingResponse:
    async def generator() -> AsyncIterator[str]:
        last_seq = 0
        idle = 0.0
        # Replay the current backlog first so a just-opened stream sees
        # the run already in progress.
        while True:
            if await request.is_disconnected():
                break
            events = INDEXER.events_since(last_seq)
            if events:
                idle = 0.0
                for event in events:
                    last_seq = max(last_seq, event["seq"])
                    yield f"event: {event['kind']}\ndata: {json.dumps(event)}\n\n"
            else:
                idle += _POLL_INTERVAL_SECONDS
                if idle >= _IDLE_KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keepalive\n\n"
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
