"""``python -m benchmarks.search_quality.reranker_evaluation``

Search Quality Improvement Plan, Phase 11's own promotion-gate
measurement: >= 5% MRR improvement AND an acceptable warm-query p95
latency increase (see CHANGELOG.md's Phase 11 entry for the actual
numbers this produced, and why the feature still ships ``enabled=false``
regardless of the result -- promotion to default-on is explicitly a later
decision, not this phase's).

``evaluator.py``/``report.py`` never exercise this: their golden-query
quality section runs every query through plain ``retrieval/lexical.
search`` only (see ``evaluator.py``'s own module docstring -- "Deliberately
lexical-only"), so a change to ``retrieval/merger.py``/``retrieval/
reranker.py``/``retrieval/neural_reranker.py`` is invisible to it. This is
therefore a separate, standalone script (not wired into ``report.py``'s
committed ``baseline_report.json``/``.md``, which stays scoped to Phase
0's original lexical-only measurement) that runs the same golden-query set
through the *full* hybrid pipeline instead:

    lexical.search + semantic.semantic_search -> merger.merge ->
    reranker.rerank (RRF)  [the "without" arm]
                 |
                 v
    neural_reranker.rerank_hits (top_n)        [the "with" arm]

and reports MRR/latency for both arms plus their delta. Requires real
network + model access (the real embedding model and the real cross-
encoder both get downloaded/cached on first run) -- this project's default
test suite never depends on this script; see ``tests/unit/
test_neural_reranker.py``'s ``reranker_model``-marked coverage for what
runs offline instead.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks.search.quality import reciprocal_rank, result_key
from benchmarks.search_quality.evaluator import load_golden_queries
from benchmarks.search_quality.fixture_project import write_project
from typer.testing import CliRunner

from ragpilot.cli.main import app
from ragpilot.core.config import SearchRerankerConfig
from ragpilot.core.lifecycle import AppContext
from ragpilot.retrieval import lexical, merger, neural_reranker, reranker, semantic

DEFAULT_TOP_N = SearchRerankerConfig().top_n
DEFAULT_WARM_REPEATS = 10


@dataclass(frozen=True)
class _Arm:
    """One ranking arm's (RRF-only, or RRF + neural) per-query results:
    every query's ranked ``(kind, path)`` key list (for MRR) and every
    query's own wall-clock cost of *only* this arm's extra work (for the
    reranker's added-latency measurement -- RRF/merge cost is shared by
    both arms and excluded from the delta on purpose).
    """

    name: str
    ranked_ids_by_query: dict[str, list[str]]
    stage_ms_by_query: dict[str, float]


def _index_fixture_project(base: Path) -> tuple[Path, Path]:
    """Indexes a fresh copy of the fixture project with ``search.semantic``
    on (real embeddings) -- returns ``(home, project_root)``.
    """
    home = base / "home"
    root = base / "project"
    write_project(root)

    previous_home = os.environ.get("RAGPILOT_HOME")
    previous_semantic = os.environ.get("RAGPILOT_SEARCH__SEMANTIC")
    os.environ["RAGPILOT_HOME"] = str(home)
    os.environ["RAGPILOT_SEARCH__SEMANTIC"] = "true"
    try:
        runner = CliRunner()
        for args in (["init"], ["source", "add", str(root)], ["index"]):
            result = runner.invoke(app, args)
            if result.exit_code != 0:
                raise RuntimeError(f"`ragpilot {' '.join(args)}` failed: {result.output}")
    finally:
        if previous_home is None:
            os.environ.pop("RAGPILOT_HOME", None)
        else:
            os.environ["RAGPILOT_HOME"] = previous_home
        if previous_semantic is None:
            os.environ.pop("RAGPILOT_SEARCH__SEMANTIC", None)
        else:
            os.environ["RAGPILOT_SEARCH__SEMANTIC"] = previous_semantic
    return home, root


def run_comparison(
    ctx: AppContext,
    *,
    project_root: Path,
    golden: list[dict[str, Any]],
    top_n: int = DEFAULT_TOP_N,
    warm_repeats: int = DEFAULT_WARM_REPEATS,
) -> dict[str, Any]:
    """Runs every golden query through the full hybrid pipeline once to
    get its RRF-fused candidate pool, then measures both arms (see the
    module docstring) against that same pool -- so any MRR/latency delta
    reflects the neural pass itself, never a difference in what RRF found.

    Latency for the neural arm is measured warm (``warm_repeats`` repeats
    per query, after one untimed warmup call that pays the one-time model
    load) -- the same "cold vs. warm" distinction ``report.py``'s own
    ``_cold_warm_semantic_section`` draws, and the one this phase's
    promotion gate actually cares about (a real query-serving process
    loads the model once, not once per query).
    """
    search_config = ctx.config.search

    per_query_pool: dict[str, tuple[str, list[reranker.RankedHit]]] = {}
    for item in golden:
        query = item["query"]
        lexical_results = lexical.search(ctx, query, limit=merger.MAX_LEXICAL_CANDIDATES)
        semantic_result = semantic.semantic_search(
            ctx, query, config=search_config, candidate_k=merger.MAX_SEMANTIC_CANDIDATES
        )
        candidates = merger.merge(lexical_results, list(semantic_result.results))
        ranked = reranker.rerank(candidates, limit=max(top_n, 10))
        per_query_pool[query] = (item["category"], ranked)

    def _ids(ranked: list[reranker.RankedHit]) -> list[str]:
        keys = []
        for hit in ranked:
            try:
                rel = Path(hit.candidate.path).relative_to(project_root).as_posix()
            except ValueError:
                rel = hit.candidate.path
            keys.append(result_key(hit.candidate.kind, rel))
        return keys

    rrf_ids_by_query = {query: _ids(ranked) for query, (_cat, ranked) in per_query_pool.items()}

    # One untimed warmup call so the model-load cost (a real, one-time
    # network+disk cost this benchmark is not trying to measure) never
    # leaks into the "warm" latency samples below.
    warmup_query, (_cat, warmup_ranked) = next(iter(per_query_pool.items()))
    neural_reranker.rerank_hits(warmup_query, warmup_ranked, top_n=top_n)

    neural_ids_by_query: dict[str, list[str]] = {}
    stage_ms_by_query: dict[str, float] = {}
    for query, (_cat, ranked) in per_query_pool.items():
        samples: list[float] = []
        reordered: list[reranker.RankedHit] = ranked
        for _ in range(warm_repeats):
            started = time.perf_counter()
            reordered = neural_reranker.rerank_hits(query, ranked, top_n=top_n)
            samples.append((time.perf_counter() - started) * 1000)
        neural_ids_by_query[query] = _ids(reordered)
        stage_ms_by_query[query] = statistics.median(samples)

    rrf_arm = _Arm(name="rrf_only", ranked_ids_by_query=rrf_ids_by_query, stage_ms_by_query={})
    neural_arm = _Arm(
        name="rrf_plus_neural",
        ranked_ids_by_query=neural_ids_by_query,
        stage_ms_by_query=stage_ms_by_query,
    )

    def _mrr(arm: _Arm) -> float:
        scores = []
        for item in golden:
            relevant = {result_key(e["kind"], e["path"]) for e in item["expected"]}
            scores.append(reciprocal_rank(arm.ranked_ids_by_query[item["query"]], relevant))
        return statistics.mean(scores)

    rrf_mrr = _mrr(rrf_arm)
    neural_mrr = _mrr(neural_arm)
    mrr_delta_pct = ((neural_mrr - rrf_mrr) / rrf_mrr * 100) if rrf_mrr > 0 else float("nan")

    all_stage_ms = sorted(stage_ms_by_query.values())
    p95_index = max(0, min(len(all_stage_ms) - 1, round(0.95 * (len(all_stage_ms) - 1))))
    warm_p95_ms = all_stage_ms[p95_index] if all_stage_ms else 0.0

    return {
        "queries_evaluated": len(golden),
        "top_n": top_n,
        "warm_repeats": warm_repeats,
        "mrr": {
            "rrf_only": round(rrf_mrr, 4),
            "rrf_plus_neural": round(neural_mrr, 4),
            "delta_pct": round(mrr_delta_pct, 2),
            "meets_5pct_gate": mrr_delta_pct >= 5.0,
        },
        "neural_rerank_stage_latency_ms": {
            "warm_p50": round(statistics.median(all_stage_ms), 3) if all_stage_ms else 0.0,
            "warm_p95": round(warm_p95_ms, 3),
        },
    }


def main(argv: list[str] | None = None) -> int:
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--warm-repeats", type=int, default=DEFAULT_WARM_REPEATS)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "reranker_evaluation.json",
    )
    args = parser.parse_args(argv)

    golden = load_golden_queries()
    with tempfile.TemporaryDirectory(prefix="ragpilot-reranker-eval-") as tmp:
        base = Path(tmp)
        home, root = _index_fixture_project(base)
        ctx = AppContext.bootstrap(home=home, cwd=root.parent)
        try:
            report = run_comparison(
                ctx,
                project_root=root,
                golden=golden,
                top_n=args.top_n,
                warm_repeats=args.warm_repeats,
            )
        finally:
            ctx.close()

    report["schema_version"] = 1
    report["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    mrr = report["mrr"]
    latency = report["neural_rerank_stage_latency_ms"]
    print(
        f"MRR: rrf_only={mrr['rrf_only']} rrf_plus_neural={mrr['rrf_plus_neural']} "
        f"delta={mrr['delta_pct']}% (>=5% gate: {mrr['meets_5pct_gate']})"
    )
    print(
        f"Neural rerank stage latency: warm p50={latency['warm_p50']} ms, "
        f"warm p95={latency['warm_p95']} ms"
    )
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
