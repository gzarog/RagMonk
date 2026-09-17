"""``pytest -m benchmark_search`` (blueprint section 35): generates a
synthetic corpus and runs the benchmark query set against it, printing a
latency report. Excluded from the default suite (see pyproject.toml's
``addopts``) since its numbers are only meaningful on real, unshared
hardware -- this test asserts the pipeline *runs correctly* (every
category finds its known fixture, hit counts are sane) rather than
hard-gating on the blueprint's millisecond targets, which a shared CI
runner cannot reliably meet or miss meaningfully.

Corpus size defaults to ``small`` (fast enough for routine use); set
``RAGMONK_BENCHMARK_SIZE`` to run a larger one locally, e.g.::

    RAGMONK_BENCHMARK_SIZE=medium pytest -m benchmark_search -q -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from benchmarks.search import corpus, queries, runner
from benchmarks.search.fake_embedder import fake_embed_texts

from ragmonk.retrieval import embedder


@pytest.mark.benchmark_search
def test_benchmark_suite_runs_and_finds_known_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Query-time embedding must never hit the real model/network here --
    # see fake_embedder.py's module docstring. Corpus generation itself
    # writes vectors directly (never calls embed_texts), so this only
    # needs to be in place before the benchmark queries run.
    monkeypatch.setattr(embedder, "embed_texts", fake_embed_texts)

    size = os.environ.get("RAGMONK_BENCHMARK_SIZE", "small")
    generated = corpus.generate(tmp_path, size_name=size, seed=0)
    try:
        query_set = queries.build_queries(generated)
        results = runner.run_benchmark(generated, query_set, repeats=5)
    finally:
        generated.close()

    report = runner.format_report(generated, results)
    print("\n" + report)

    assert len(results) == len(query_set)
    for result in results:
        assert result.hits > 0, f"{result.category!r} found nothing for query {result.query!r}"
        assert result.p50_ms >= 0
        assert result.p95_ms >= result.p50_ms

    missed = [r for r in results if not r.meets_target]
    if missed:
        names = ", ".join(
            f"{r.category} (p50={r.p50_ms:.1f}ms, p95={r.p95_ms:.1f}ms)" for r in missed
        )
        print(f"\nNote: missed blueprint targets on this hardware: {names}")
