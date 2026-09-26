"""Lightweight sanity coverage for the ``benchmarks/server_indexing``
harness itself: corpus generation, metric collection, and incremental
change detection, run on a small (~30 file) corpus against a fake/mock
``opensearch-py`` client (``tests/unit/_fake_opensearch.py``) -- never a
real cluster and never 2,000+ files. This is what CI runs on every PR;
the actual full-scale real-cluster benchmark
(``python -m benchmarks.server_indexing``) is run manually/on demand,
documented in ``docs/indexing_benchmarks.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.server_indexing import corpus as corpus_module
from benchmarks.server_indexing.bulk_capture import capture_bulk_stats
from benchmarks.server_indexing.metrics import (
    IncrementalRunMetrics,
    IndexRunMetrics,
    QueryLatencyStats,
    percentile,
)
from benchmarks.server_indexing.runner import (
    build_incremental_metrics,
    build_server_config,
    measure_query_latency,
    representative_queries,
    run_indexing_pass,
)
from tests.unit._fake_opensearch import FakeOpenSearch

from ragmonk.backends import opensearch_bulk

# -- corpus generation ------------------------------------------------


def test_generate_corpus_creates_expected_file_count_and_cross_file_calls(
    tmp_path: Path,
) -> None:
    files = corpus_module.generate_corpus(tmp_path, 30)
    assert len(files) >= 30
    py_files = [f for f in files if f.language == "python"]
    assert len(py_files) >= 20

    # Module 5 must reference module 4's helper -- the whole point of
    # this generator versus the local-backend suite's isolated modules.
    mod5 = tmp_path / "pkg" / "mod_5.py"
    assert mod5.exists()
    text = mod5.read_text()
    assert "from pkg.mod_4 import helper_4" in text
    assert "helper_4(" in text
    compile(text, str(mod5), "exec")  # syntactically valid


def test_generate_corpus_is_deterministic(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    corpus_module.generate_corpus(a, 20, seed=42)
    corpus_module.generate_corpus(b, 20, seed=42)
    rel_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    rel_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert rel_a == rel_b
    for ra in rel_a:
        assert (a / ra).read_text() == (b / ra).read_text()


def test_apply_incremental_changes_touches_expected_fractions(tmp_path: Path) -> None:
    files = corpus_module.generate_corpus(tmp_path, 100)
    changes = corpus_module.apply_incremental_changes(
        tmp_path, files, modify_pct=0.02, add_pct=0.01, delete_pct=0.01, rename_pct=0.01
    )
    assert len(changes["modified"]) >= 1
    assert len(changes["added"]) >= 1
    assert len(changes["deleted"]) >= 1
    assert len(changes["renamed"]) >= 1
    # Deleted files must actually be gone; added/renamed must exist.
    for rel in changes["deleted"]:
        assert not (tmp_path / rel).exists()
    for rel in changes["added"] + changes["renamed"]:
        assert (tmp_path / rel).exists()


# -- metrics ------------------------------------------------------------


def test_percentile_basic() -> None:
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([], 50) == 0.0
    assert percentile([7.0], 95) == 7.0


def test_query_latency_stats_from_samples() -> None:
    stats = QueryLatencyStats.from_samples("lexical", [1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats.samples == 5
    assert stats.max_ms == 5.0
    assert stats.p50_ms == 3.0
    data = stats.as_dict()
    assert data["label"] == "lexical"


def test_incremental_metrics_flags_full_rewrite() -> None:
    idx = IndexRunMetrics(scenario="incremental", files_scanned=1000, files_indexed=950)
    incr = IncrementalRunMetrics(
        corpus_size=1000,
        files_modified_on_disk=10,
        files_added_on_disk=5,
        files_deleted_on_disk=5,
        files_renamed_on_disk=5,
        index_metrics=idx,
    )
    assert incr.looks_like_full_rewrite is True

    idx_small = IndexRunMetrics(scenario="incremental", files_scanned=1000, files_indexed=22)
    incr_small = IncrementalRunMetrics(
        corpus_size=1000,
        files_modified_on_disk=10,
        files_added_on_disk=5,
        files_deleted_on_disk=5,
        files_renamed_on_disk=5,
        index_metrics=idx_small,
    )
    assert incr_small.looks_like_full_rewrite is False


# -- bulk capture ---------------------------------------------------------


def test_bulk_capture_counts_actions_and_batches() -> None:
    from ragmonk.core.config import BulkConfig

    fake = FakeOpenSearch()
    config = BulkConfig(max_actions=5, max_bytes=1_000_000, concurrency=1, max_retries=1)
    actions = [
        opensearch_bulk.BulkAction(
            op="index", index="bench-files", doc_id=f"id{i}", source={"n": i}
        )
        for i in range(12)
    ]
    with capture_bulk_stats(opensearch_bulk) as stats:
        result = opensearch_bulk.run_bulk(fake, actions, config)

    assert result.succeeded == 12
    assert stats.bulk_actions == 12
    assert stats.initial_batches == 3  # ceil(12 / 5)
    assert stats.bulk_requests == 3  # no retries needed against a healthy fake
    assert stats.terminal_failures == 0
    assert stats.max_batch_size == 5


# -- end-to-end harness smoke test (fake backend, small corpus) ---------


def test_harness_smoke_run_against_fake_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the CI-safe sanity check: it exercises the FULL benchmark
    harness (corpus generation -> real ``run_source_pass`` pipeline ->
    metric collection -> incremental rerun) end to end, but against a
    fake in-memory OpenSearch client and a tiny (~30 file) corpus. It
    proves the harness's own logic is correct; it is NOT evidence for
    the real-cluster, 2000+-file benchmark requirement.
    """
    fake = FakeOpenSearch()
    monkeypatch.setattr("ragmonk.backends.opensearch.build_client", lambda **_kwargs: fake)

    home = tmp_path / "home"
    source_path = tmp_path / "corpus"
    home.mkdir(parents=True)

    files = corpus_module.generate_corpus(source_path, 30)
    config = build_server_config("opensearch", "http://fake.test:9200", index_prefix="smoketest")

    metrics, ctx, registry = run_indexing_pass(
        home=home, source_path=source_path, config=config, scenario="cold_index"
    )
    try:
        assert metrics.files_scanned >= 30
        assert metrics.files_indexed >= 30
        assert metrics.files_failed == 0
        assert metrics.bulk_actions > 0
        assert metrics.entity_docs_after > metrics.entity_docs_before

        queries = representative_queries(30)
        lexical_stats = measure_query_latency(ctx, queries, label="lexical", repeats=1)
        assert lexical_stats.samples == len(queries)

        changes = corpus_module.apply_incremental_changes(source_path, files)
        incr_metrics, _ctx2, _registry2 = run_indexing_pass(
            home=home,
            source_path=source_path,
            config=config,
            scenario="incremental",
            ctx=ctx,
            registry=registry,
        )
        incremental = build_incremental_metrics(
            corpus_size=len(files), changes=changes, index_metrics=incr_metrics
        )
        # The whole point of the incremental variant: it must NOT touch
        # anywhere near the full corpus.
        assert incremental.index_metrics.files_indexed < len(files) // 2
        assert incremental.looks_like_full_rewrite is False
    finally:
        ctx.close()
