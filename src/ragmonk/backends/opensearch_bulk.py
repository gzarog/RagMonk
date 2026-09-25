"""Bounded bulk indexing for the OpenSearch adapter.

Storage backend abstraction plan, Phase 4: every write path
(``publish_code``/``publish_document``/``publish_embeddings``/
``publish_links``/``upsert_file``/``clear_source``) goes through
:func:`run_bulk` -- never a per-entity/per-chunk HTTP request. Batches
are bounded by :class:`~ragmonk.core.config.BulkConfig`'s
``max_actions``/``max_bytes``, dispatched with up to ``concurrency``
batches in flight at once, and a batch's individually-failed items are
retried with exponential backoff up to ``max_retries`` times before
being surfaced as a typed, detailed failure report.

No ``opensearch-py`` import at module scope -- ``run_bulk`` takes an
already-constructed client and calls its ``bulk`` helper by attribute,
so this module never imports ``opensearchpy`` itself.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ragmonk.core.config import BulkConfig
from ragmonk.core.errors import DatabaseError
from ragmonk.telemetry.logging import get_logger, log_event

_logger = get_logger("backends.opensearch_bulk")

# HTTP/bulk-item statuses worth retrying: throttling and transient
# conflict/server errors. A genuine mapping/validation error (400) is not
# retried -- retrying it would just fail identically every time.
_RETRYABLE_STATUSES = {409, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class BulkAction:
    """One bulk action: ``op`` is ``"index"`` (full document replace),
    ``"update"`` (partial merge -- e.g. layering an ``embedding`` field
    onto a content document ``publish_code``/``publish_document`` already
    wrote, without wiping its other fields), or ``"delete"``. ``doc_id``
    is always caller-supplied and deterministic (see ``opensearch_ids.py``)
    -- never left for OpenSearch to generate. ``source`` is the document
    body for ``"index"``, or the partial-merge fields for ``"update"``
    (ignored for ``"delete"``).
    """

    op: str
    index: str
    doc_id: str
    source: dict[str, Any] | None = None


@dataclass(slots=True)
class BulkFailure:
    index: str
    doc_id: str
    op: str
    status: int
    reason: str


@dataclass(slots=True)
class BulkResult:
    succeeded: int = 0
    failed: list[BulkFailure] = field(default_factory=list)


class BulkIndexError(DatabaseError):
    """Raised when one or more bulk actions failed and retries were
    exhausted. Carries per-item detail (index, id, op, status, reason)
    but never a credential value -- item bodies/reasons come straight
    from OpenSearch's own bulk response, which never echoes auth.
    """

    def __init__(self, failures: list[BulkFailure]) -> None:
        self.failures = failures
        detail = "; ".join(
            f"{f.op} {f.index}/{f.doc_id} -> {f.status} {f.reason}" for f in failures[:10]
        )
        more = "" if len(failures) <= 10 else f" (+{len(failures) - 10} more)"
        super().__init__(
            f"OpenSearch bulk indexing failed for {len(failures)} item(s): {detail}{more}"
        )


def _action_line(action: BulkAction) -> list[dict[str, Any]]:
    if action.op == "delete":
        return [{"delete": {"_index": action.index, "_id": action.doc_id}}]
    if action.op == "update":
        meta = {"update": {"_index": action.index, "_id": action.doc_id}}
        return [meta, {"doc": action.source or {}, "doc_as_upsert": True}]
    meta = {"index": {"_index": action.index, "_id": action.doc_id}}
    return [meta, action.source or {}]


def _action_bytes(action: BulkAction) -> int:
    return sum(len(json.dumps(line, default=str)) for line in _action_line(action))


def _batch(
    actions: list[BulkAction], max_actions: int, max_bytes: int
) -> Iterable[list[BulkAction]]:
    batch: list[BulkAction] = []
    batch_bytes = 0
    for action in actions:
        action_bytes = _action_bytes(action)
        if batch and (len(batch) >= max_actions or batch_bytes + action_bytes > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(action)
        batch_bytes += action_bytes
    if batch:
        yield batch


def _send_batch(client: Any, batch: list[BulkAction]) -> tuple[int, list[BulkFailure]]:
    body: list[dict[str, Any]] = []
    for action in batch:
        body.extend(_action_line(action))

    response = client.bulk(body=body)
    succeeded = 0
    failures: list[BulkFailure] = []
    for action, item in zip(batch, response.get("items", []), strict=False):
        result = item.get(action.op, item.get("index", item.get("delete", {})))
        status = int(result.get("status", 500))
        if status < 300:
            succeeded += 1
            continue
        reason = ""
        error = result.get("error")
        if isinstance(error, dict):
            reason = str(error.get("reason", error.get("type", "unknown error")))
        else:
            reason = str(error or "unknown error")
        failures.append(
            BulkFailure(
                index=action.index, doc_id=action.doc_id, op=action.op, status=status, reason=reason
            )
        )
    return succeeded, failures


def _send_batch_with_retries(
    client: Any, batch: list[BulkAction], config: BulkConfig
) -> tuple[int, list[BulkFailure]]:
    """Send ``batch``, retrying only the retryable-status subset of its
    failures (exponential backoff) up to ``config.max_retries`` times.
    Returns the total succeeded count and the final list of failures
    still standing once retries are exhausted (non-retryable failures are
    included immediately, on the first attempt that produced them).
    """
    pending = list(batch)
    total_succeeded = 0
    permanent_failures: list[BulkFailure] = []
    attempt = 0

    while pending:
        succeeded, failures = _send_batch(client, pending)
        total_succeeded += succeeded

        retryable = [f for f in failures if f.status in _RETRYABLE_STATUSES]
        non_retryable = [f for f in failures if f.status not in _RETRYABLE_STATUSES]
        permanent_failures.extend(non_retryable)

        if not retryable or attempt >= config.max_retries:
            permanent_failures.extend(retryable)
            break

        attempt += 1
        retry_ids = {f.doc_id for f in retryable}
        pending = [action for action in pending if action.doc_id in retry_ids]
        time.sleep(min(2 ** (attempt - 1) * 0.1, 5.0))

    return total_succeeded, permanent_failures


def run_bulk(client: Any, actions: list[BulkAction], config: BulkConfig) -> BulkResult:
    """Send ``actions`` through OpenSearch's bulk API, batched by
    ``config.max_actions``/``config.max_bytes``, with up to
    ``config.concurrency`` batches in flight and per-item retries. Does
    NOT raise on partial failure -- returns a :class:`BulkResult` with
    every failure collected; callers that want failures to raise use
    :func:`run_bulk_or_raise`.
    """
    if not actions:
        return BulkResult()

    started = time.perf_counter()
    batches = list(_batch(actions, config.max_actions, config.max_bytes))
    result = BulkResult()

    if config.concurrency <= 1 or len(batches) <= 1:
        for batch in batches:
            succeeded, failures = _send_batch_with_retries(client, batch, config)
            result.succeeded += succeeded
            result.failed.extend(failures)
    else:
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = [
                pool.submit(_send_batch_with_retries, client, batch, config) for batch in batches
            ]
            for future in futures:
                succeeded, failures = future.result()
                result.succeeded += succeeded
                result.failed.extend(failures)

    # Structured bulk-flush timing (Storage backend abstraction plan,
    # Phase 8) -- mirrors the shape of ``retrieval/lexical.py``'s
    # ``StageTiming``/``search_with_timings`` pattern (name, count,
    # duration_ms), logged rather than returned since a bulk flush's
    # caller (``publish_code``/``publish_document``/etc.) has no
    # equivalent "timings" return slot in the ``KnowledgeBackend``
    # contract to carry it in.
    duration_ms = (time.perf_counter() - started) * 1000
    log_event(
        _logger,
        "opensearch_bulk_flush",
        actions=len(actions),
        batches=len(batches),
        succeeded=result.succeeded,
        failed=len(result.failed),
        duration_ms=round(duration_ms, 3),
    )
    return result


def run_bulk_or_raise(client: Any, actions: list[BulkAction], config: BulkConfig) -> int:
    """Convenience wrapper: runs :func:`run_bulk` and raises
    :class:`BulkIndexError` if anything failed after retries. Returns the
    number of successfully-applied actions otherwise.
    """
    result = run_bulk(client, actions, config)
    if result.failed:
        raise BulkIndexError(result.failed)
    return result.succeeded
