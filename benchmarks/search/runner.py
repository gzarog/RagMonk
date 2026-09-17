"""Latency measurement for the benchmark query set (blueprint sections
33/35/36): runs each query repeatedly against a generated corpus, reports
p50/p95 wall-clock latency, and compares against ``targets.py``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from benchmarks.search.corpus import GeneratedCorpus
from benchmarks.search.queries import BenchmarkQuery, SearchMode
from benchmarks.search.targets import LatencyTarget, target_for

from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import lexical, merger, reranker, semantic

DEFAULT_REPEATS = 20


@dataclass(frozen=True)
class LatencyResult:
    category: str
    mode: str
    query: str
    p50_ms: float
    p95_ms: float
    hits: int
    target: LatencyTarget

    @property
    def meets_target(self) -> bool:
        return self.p50_ms <= self.target.p50_ms and self.p95_ms <= self.target.p95_ms


def _percentile(sorted_samples: list[float], pct: float) -> float:
    if not sorted_samples:
        return 0.0
    rank = (pct / 100) * (len(sorted_samples) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_samples[lower]
    fraction = rank - lower
    return sorted_samples[lower] + (sorted_samples[upper] - sorted_samples[lower]) * fraction


def _measure(fn: Callable[[], int], *, repeats: int) -> tuple[float, float, int]:
    """Runs ``fn`` once as a (discarded) warmup, then ``repeats`` timed
    calls, returning ``(p50_ms, p95_ms, last_hit_count)``.
    """
    fn()
    samples: list[float] = []
    hits = 0
    for _ in range(repeats):
        started = _now_ms()
        hits = fn()
        samples.append(_now_ms() - started)
    samples.sort()
    return _percentile(samples, 50), _percentile(samples, 95), hits


def _now_ms() -> float:
    return time.perf_counter() * 1000


def _run_one(ctx: AppContext, query: BenchmarkQuery, *, limit: int) -> Callable[[], int]:
    if query.mode is SearchMode.LEXICAL:

        def run_lexical() -> int:
            return len(lexical.search(ctx, query.text, limit=limit))

        return run_lexical

    if query.mode is SearchMode.SEMANTIC:

        def run_semantic() -> int:
            result = semantic.semantic_search(
                ctx, query.text, config=ctx.config.search, limit=limit
            )
            return len(result.results)

        return run_semantic

    def run_hybrid() -> int:
        lex = lexical.search(ctx, query.text, limit=limit)
        sem = semantic.semantic_search(ctx, query.text, config=ctx.config.search, limit=limit)
        candidates = merger.merge(lex, list(sem.results))
        return len(reranker.rerank(candidates, limit=limit))

    return run_hybrid


def run_benchmark(
    corpus: GeneratedCorpus,
    queries: list[BenchmarkQuery],
    *,
    limit: int = 20,
    repeats: int = DEFAULT_REPEATS,
) -> list[LatencyResult]:
    """Measures every query's p50/p95 latency in turn.

    ``corpus.ctx`` is bootstrapped with ``search.cache.enabled=False``
    (see ``corpus.generate``), so each repeated call genuinely
    re-executes the query rather than serving a cache hit -- these
    numbers are meant to reflect the retrieval pipeline itself, not the
    cache layer benchmarked separately in ``retrieval/cache.py``'s own
    tests.
    """
    results: list[LatencyResult] = []
    for query in queries:
        fn = _run_one(corpus.ctx, query, limit=limit)
        p50, p95, hits = _measure(fn, repeats=repeats)
        results.append(
            LatencyResult(
                category=query.category,
                mode=query.mode.value,
                query=query.text,
                p50_ms=p50,
                p95_ms=p95,
                hits=hits,
                target=target_for(query.category),
            )
        )
    return results


def format_report(corpus: GeneratedCorpus, results: list[LatencyResult]) -> str:
    lines = [
        f"Corpus: {corpus.size_name} "
        f"({corpus.entity_count} entities, {corpus.document_paragraph_count} paragraphs, "
        f"generated in {corpus.generation_seconds:.2f}s)",
        "",
        f"{'Category':<18}{'Mode':<10}{'Hits':<6}{'p50 (ms)':<12}{'p95 (ms)':<12}"
        f"{'Target p50/p95':<18}{'Status'}",
    ]
    for r in results:
        status = "OK" if r.meets_target else "SLOW"
        target_str = f"{r.target.p50_ms:g}/{r.target.p95_ms:g}"
        lines.append(
            f"{r.category:<18}{r.mode:<10}{r.hits:<6}{r.p50_ms:<12.3f}{r.p95_ms:<12.3f}"
            f"{target_str:<18}{status}"
        )
    return "\n".join(lines)
