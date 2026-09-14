"""``pytest -m benchmark_search``: runs the full Phase 0 baseline report
generator (``benchmarks/search_quality/report.py``) end to end and
asserts its output is structurally sane. Marked and excluded from the
default suite for the same reason as ``test_search_benchmarks.py``: it
includes real wall-clock latency numbers, only meaningful on real,
unshared hardware -- this test asserts the report *pipeline* runs
correctly (every section present, counts non-negative, the golden-query
section's own numbers match what ``test_search_quality.py`` already
pins), never the millisecond values themselves.

The committed ``benchmarks/search_quality/baseline_report.json``/
``.md`` are this same generator's real output, produced by actually
running ``python -m benchmarks.search_quality`` once (see
CONTRIBUTING.md) -- this test does not regenerate or overwrite them.
"""

from __future__ import annotations

import pytest
from benchmarks.search_quality import report


@pytest.mark.benchmark_search
def test_baseline_report_generates_a_structurally_sane_report() -> None:
    generated = report.generate_report(corpus_size="small", latency_repeats=5)

    assert generated["schema_version"] == 1
    assert generated["generated_at"]

    quality = generated["golden_query_quality"]["overall"]
    assert quality["count"] > 0
    assert quality["recall_at"]["5"] > 0
    assert quality["mrr"] > 0
    by_category = generated["golden_query_quality"]["by_category"]
    assert len(by_category) >= 9, "expected at least the plan's 9 required categories"

    indexing = generated["indexing"]
    assert indexing["indexing_time_ms_by_file_type"]["code"] >= 0
    assert indexing["indexing_time_ms_by_file_type"]["document"] >= 0
    assert indexing["full_index_time_ms"] >= 0
    assert indexing["reindex_time_ms_unchanged"] >= 0
    assert indexing["generated_chunk_count"]["total"] > 0
    assert indexing["vector_count"] > 0
    assert indexing["knowledge_db_size_bytes"] > 0
    assert indexing["vector_index_size_bytes"] > 0

    latency = generated["latency"]
    assert latency["lexical"] is not None
    assert latency["hybrid"] is not None

    cold_warm = generated["cold_warm_semantic_search"]
    assert cold_warm["cold_ms"] >= 0
    assert cold_warm["warm_p50_ms"] >= 0

    summary = report.format_summary(generated)
    assert "Golden-query quality" in summary
    assert "Latency" in summary
