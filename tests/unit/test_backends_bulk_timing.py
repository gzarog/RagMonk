"""Storage backend abstraction plan, Phase 8: server-mode bulk indexing
emits minimal structured timing (an "*_bulk_flush" log event with an
``actions``/``duration_ms`` shape), matching local mode's own
``StageTiming``/``search_with_timings`` timing pattern's shape
(name + count + duration_ms) rather than a differently-shaped one.
"""

from __future__ import annotations

import logging

import pytest
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends.elasticsearch_bulk import BulkAction as EsBulkAction
from ragmonk.backends.elasticsearch_bulk import run_bulk as es_run_bulk
from ragmonk.backends.opensearch_bulk import BulkAction as OsBulkAction
from ragmonk.backends.opensearch_bulk import run_bulk as os_run_bulk
from ragmonk.core.config import BulkConfig


def test_opensearch_bulk_flush_logs_structured_timing(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeOpenSearch()
    actions = [OsBulkAction(op="index", index="i", doc_id=f"d{i}", source={}) for i in range(3)]
    with caplog.at_level(logging.INFO, logger="ragmonk"):
        result = os_run_bulk(fake, actions, BulkConfig())

    assert result.succeeded == 3
    events = [r for r in caplog.records if getattr(r, "event", None) == "opensearch_bulk_flush"]
    assert len(events) == 1
    record = events[0]
    assert getattr(record, "actions") == 3  # noqa: B009
    assert getattr(record, "succeeded") == 3  # noqa: B009
    duration_ms = getattr(record, "duration_ms")  # noqa: B009
    assert isinstance(duration_ms, float)
    assert duration_ms >= 0


def test_elasticsearch_bulk_flush_logs_structured_timing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.unit._fake_elasticsearch import FakeElasticsearch

    fake = FakeElasticsearch()
    actions = [EsBulkAction(op="index", index="i", doc_id=f"d{i}", source={}) for i in range(3)]
    with caplog.at_level(logging.INFO, logger="ragmonk"):
        result = es_run_bulk(fake, actions, BulkConfig())

    assert result.succeeded == 3
    events = [
        r for r in caplog.records if getattr(r, "event", None) == "elasticsearch_bulk_flush"
    ]
    assert len(events) == 1
    record = events[0]
    assert getattr(record, "actions") == 3  # noqa: B009
    assert getattr(record, "succeeded") == 3  # noqa: B009
    duration_ms = getattr(record, "duration_ms")  # noqa: B009
    assert isinstance(duration_ms, float)
    assert duration_ms >= 0
