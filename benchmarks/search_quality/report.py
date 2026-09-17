"""``python -m benchmarks.search_quality [--out DIR]``

Builds one structured baseline report (Phase 0 of the search-quality
improvement plan) combining everything a later phase needs to diff its
own changes against:

* golden-query Recall@1/3/5/10, MRR, and NDCG@10, overall and per
  category (``evaluator.py``, against the real, hand-written fixture
  project -- deterministic, meaningful on any machine, including CI).
* lexical/semantic/hybrid search latency p50/p95, and cold-vs-warm
  semantic search latency (reuses ``benchmarks/search``'s existing
  synthetic-corpus latency infra -- meaningful only on real, unshared
  hardware, like that suite's own numbers already are).
* indexing time by file type, re-indexing (no-op) time, generated chunk
  count (code entities + document sections), vector count, knowledge DB
  size on disk, and vector index size on disk -- measured directly
  against the fixture project.

Running this script end to end (``python -m benchmarks.search_quality``)
is what produced the committed ``baseline_report.json``/
``baseline_report.md`` in this same directory -- see
``tests/integration/test_search_quality_report.py`` for the
``pytest -m benchmark_search`` coverage that exercises this module
without hard-gating on its (hardware-dependent) latency numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from benchmarks.search import corpus as bench_corpus
from benchmarks.search import queries as bench_queries
from benchmarks.search import runner as bench_runner
from benchmarks.search.fake_embedder import fake_embed_texts
from benchmarks.search_quality.evaluator import evaluate_golden_queries, load_golden_queries
from benchmarks.search_quality.fixture_project import write_project
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.retrieval import embedder
from ragmonk.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    vector_items_repo,
)

DEFAULT_CORPUS_SIZE = "small"
DEFAULT_LATENCY_REPEATS = 20


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _timed_cli(runner: CliRunner, args: list[str]) -> tuple[int, float]:
    """Runs one CLI command, returning ``(exit_code, elapsed_ms)``."""
    started = time.perf_counter()
    result = runner.invoke(app, args)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if result.exit_code != 0:
        raise RuntimeError(f"`ragmonk {' '.join(args)}` failed: {result.output}")
    return result.exit_code, elapsed_ms


@dataclass(frozen=True)
class _IndexedProject:
    home: Path
    root: Path
    project_id: str


def _index_fresh_project(
    base: Path, *, name: str, populate: Any, semantic: bool = False
) -> tuple[_IndexedProject, float]:
    """Bootstraps a brand new ``RAGMONK_HOME``/source root under
    ``base``, populates it via ``populate(root)``, and times one
    ``ragmonk index`` run. Each project gets its own home directory so
    "indexing time by file type" measurements never share a knowledge.db
    (and its cold caches) with each other.
    """
    home = base / f"{name}_home"
    root = base / f"{name}_project"
    populate(root)

    previous_home = os.environ.get("RAGMONK_HOME")
    previous_semantic = os.environ.get("RAGMONK_SEARCH__SEMANTIC")
    os.environ["RAGMONK_HOME"] = str(home)
    if semantic:
        os.environ["RAGMONK_SEARCH__SEMANTIC"] = "true"
    elif "RAGMONK_SEARCH__SEMANTIC" in os.environ:
        del os.environ["RAGMONK_SEARCH__SEMANTIC"]
    try:
        runner = CliRunner()
        _timed_cli(runner, ["init"])
        _timed_cli(runner, ["source", "add", str(root)])
        _, elapsed_ms = _timed_cli(runner, ["index"])
    finally:
        if previous_home is None:
            os.environ.pop("RAGMONK_HOME", None)
        else:
            os.environ["RAGMONK_HOME"] = previous_home
        if previous_semantic is None:
            os.environ.pop("RAGMONK_SEARCH__SEMANTIC", None)
        else:
            os.environ["RAGMONK_SEARCH__SEMANTIC"] = previous_semantic

    project_id = paths.project_id_for_path(root)
    return _IndexedProject(home=home, root=root, project_id=project_id), elapsed_ms


def _write_code_only(root: Path) -> None:
    full = root.parent / f"{root.name}_full_src"
    write_project(full)
    shutil.copytree(full / "services", root / "services")
    shutil.copytree(full / "consumers", root / "consumers")
    shutil.copytree(full / "workers", root / "workers")
    shutil.rmtree(full)


def _write_docs_only(root: Path) -> None:
    full = root.parent / f"{root.name}_full_src"
    write_project(full)
    shutil.copytree(full / "docs", root / "docs")
    shutil.rmtree(full)


def _quality_section(base: Path) -> dict[str, Any]:
    golden = load_golden_queries()
    indexed, _ = _index_fresh_project(base, name="quality", populate=write_project)
    ctx = AppContext.bootstrap(home=indexed.home, cwd=indexed.root.parent)
    try:
        report = evaluate_golden_queries(ctx, project_root=indexed.root, golden=golden)
    finally:
        ctx.close()
    return report.to_dict()


def _indexing_section(base: Path) -> dict[str, Any]:
    # search.semantic=True (the "mixed" project below) makes `ragmonk
    # index` compute real embeddings -- must never hit the real model/
    # network here, same rule `benchmarks/search`'s own latency suite
    # follows (see fake_embedder.py's module docstring).
    real_embed_texts = embedder.embed_texts
    embedder.embed_texts = fake_embed_texts
    try:
        code_project, code_ms = _index_fresh_project(
            base, name="code_only", populate=_write_code_only
        )
        docs_project, docs_ms = _index_fresh_project(
            base, name="docs_only", populate=_write_docs_only
        )
        mixed_project, full_ms = _index_fresh_project(
            base, name="mixed", populate=write_project, semantic=True
        )

        previous_home = os.environ.get("RAGMONK_HOME")
        os.environ["RAGMONK_HOME"] = str(mixed_project.home)
        try:
            runner = CliRunner()
            _, reindex_ms = _timed_cli(runner, ["index"])
        finally:
            if previous_home is None:
                os.environ.pop("RAGMONK_HOME", None)
            else:
                os.environ["RAGMONK_HOME"] = previous_home
    finally:
        embedder.embed_texts = real_embed_texts

    stats_ctx = AppContext.bootstrap(home=mixed_project.home, cwd=mixed_project.root.parent)
    try:
        conn = stats_ctx.project_conn(mixed_project.project_id)
        entity_count = entities_repo.count_all(conn)
        document_count = documents_repo.count_all(conn)
        section_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM document_sections GROUP BY kind"
        ).fetchall()
        sections_by_kind = {row["kind"]: row["n"] for row in section_rows}
        section_count = sum(sections_by_kind.values())
        vector_count = vector_items_repo.count_all(conn, model_id=embedder.EMBEDDING_MODEL_ID)
        embedding_count = embeddings_repo.count_all(conn)
    finally:
        stats_ctx.close()

    db_bytes = _file_size(paths.project_db_path(mixed_project.project_id, mixed_project.home))
    vector_index_bytes = _file_size(
        paths.project_vector_index_path(mixed_project.project_id, mixed_project.home)
    )

    return {
        "indexing_time_ms_by_file_type": {
            "code": round(code_ms, 3),
            "document": round(docs_ms, 3),
        },
        "full_index_time_ms": round(full_ms, 3),
        "reindex_time_ms_unchanged": round(reindex_ms, 3),
        "generated_chunk_count": {
            "code_entities": entity_count,
            "document_sections": section_count,
            "document_sections_by_kind": sections_by_kind,
            "total": entity_count + section_count,
        },
        "document_count": document_count,
        "vector_count": vector_count,
        "embedding_count": embedding_count,
        "knowledge_db_size_bytes": db_bytes,
        "vector_index_size_bytes": vector_index_bytes,
        "note": (
            "Measured against the small, hand-written fixture project (a handful of "
            "files) -- real, honestly-obtained numbers, but indicative of pipeline "
            "correctness/overhead only, not representative of large-corpus "
            "performance. See the latency section for synthetic-corpus numbers."
        ),
    }


def _latency_section(
    base: Path, *, corpus_size: str, repeats: int
) -> dict[str, Any]:
    real_embed_texts = embedder.embed_texts
    embedder.embed_texts = fake_embed_texts
    generated = bench_corpus.generate(base / "latency", size_name=corpus_size, seed=0)
    try:
        query_set = bench_queries.build_queries(generated)
        results = bench_runner.run_benchmark(generated, query_set, repeats=repeats)
    finally:
        embedder.embed_texts = real_embed_texts
        generated.close()

    by_mode: dict[str, list[bench_runner.LatencyResult]] = {}
    for result in results:
        by_mode.setdefault(result.mode, []).append(result)

    def _mode_summary(mode: str) -> dict[str, float] | None:
        group = by_mode.get(mode)
        if not group:
            return None
        return {
            "p50_ms": round(mean(r.p50_ms for r in group), 3),
            "p95_ms": round(max(r.p95_ms for r in group), 3),
        }

    per_category = {
        result.category: {
            "mode": result.mode,
            "p50_ms": round(result.p50_ms, 3),
            "p95_ms": round(result.p95_ms, 3),
            "meets_target": result.meets_target,
        }
        for result in results
    }

    return {
        "corpus_size": corpus_size,
        "repeats": repeats,
        "lexical": _mode_summary("lexical"),
        "semantic": _mode_summary("semantic"),
        "hybrid": _mode_summary("hybrid"),
        "by_category": per_category,
    }


def _cold_warm_semantic_section(base: Path, *, repeats: int) -> dict[str, Any]:
    real_embed_texts = embedder.embed_texts
    embedder.embed_texts = fake_embed_texts
    generated = bench_corpus.generate(base / "cold_warm", size_name=DEFAULT_CORPUS_SIZE, seed=1)
    try:
        from ragmonk.retrieval import semantic as semantic_module

        query = generated.known.document_title.lower()

        def run_once() -> int:
            result = semantic_module.semantic_search(
                generated.ctx, query, config=generated.ctx.config.search, limit=20
            )
            return len(result.results)

        started = time.perf_counter()
        cold_hits = run_once()
        cold_ms = (time.perf_counter() - started) * 1000

        warm_samples: list[float] = []
        warm_hits = 0
        for _ in range(repeats):
            started = time.perf_counter()
            warm_hits = run_once()
            warm_samples.append((time.perf_counter() - started) * 1000)
        warm_samples.sort()
        warm_p50 = bench_runner._percentile(warm_samples, 50)
        warm_p95 = bench_runner._percentile(warm_samples, 95)
    finally:
        embedder.embed_texts = real_embed_texts
        generated.close()

    return {
        "cold_ms": round(cold_ms, 3),
        "cold_hits": cold_hits,
        "warm_p50_ms": round(warm_p50, 3),
        "warm_p95_ms": round(warm_p95, 3),
        "warm_hits": warm_hits,
        "note": (
            "'Cold' is the first semantic_search call in this process (loads the "
            "persistent ANN index from disk); 'warm' repeats the same query against "
            "the process-cached in-memory index (see retrieval/ann.py's "
            "get_cached_backend)."
        ),
    }


def generate_report(
    *,
    corpus_size: str = DEFAULT_CORPUS_SIZE,
    latency_repeats: int = DEFAULT_LATENCY_REPEATS,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ragmonk-search-quality-baseline-") as tmp:
        base = Path(tmp)
        quality = _quality_section(base)
        indexing = _indexing_section(base)
        latency = _latency_section(base, corpus_size=corpus_size, repeats=latency_repeats)
        cold_warm_semantic = _cold_warm_semantic_section(base, repeats=latency_repeats)

    return {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "golden_query_quality": quality,
        "indexing": indexing,
        "latency": latency,
        "cold_warm_semantic_search": cold_warm_semantic,
    }


def format_summary(report: dict[str, Any]) -> str:
    quality = report["golden_query_quality"]["overall"]
    indexing = report["indexing"]
    latency = report["latency"]
    cold_warm = report["cold_warm_semantic_search"]

    lines = [
        "# RagMonk search quality baseline (Phase 0)",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Golden-query quality (overall)",
        "",
        f"- Recall@1: {quality['recall_at']['1']}",
        f"- Recall@3: {quality['recall_at']['3']}",
        f"- Recall@5: {quality['recall_at']['5']}",
        f"- Recall@10: {quality['recall_at']['10']}",
        f"- MRR: {quality['mrr']}",
        f"- NDCG@10: {quality['ndcg_at_10']}",
        f"- Queries evaluated: {quality['count']}",
        "",
        "## Golden-query quality (by category)",
        "",
        "| Category | N | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for category, summary in sorted(report["golden_query_quality"]["by_category"].items()):
        r = summary["recall_at"]
        lines.append(
            f"| {category} | {summary['count']} | {r['1']} | {r['3']} | {r['5']} | {r['10']} | "
            f"{summary['mrr']} | {summary['ndcg_at_10']} |"
        )

    lines += [
        "",
        "## Latency (synthetic corpus)",
        "",
        f"Corpus size: {latency['corpus_size']}, repeats: {latency['repeats']}",
        "",
    ]
    for mode in ("lexical", "semantic", "hybrid"):
        summary = latency[mode]
        if summary is None:
            continue
        lines.append(f"- {mode}: p50={summary['p50_ms']} ms, p95={summary['p95_ms']} ms")

    lines += [
        "",
        "## Cold vs warm semantic search",
        "",
        f"- Cold (first call, loads ANN index from disk): {cold_warm['cold_ms']} ms",
        f"- Warm p50: {cold_warm['warm_p50_ms']} ms, p95: {cold_warm['warm_p95_ms']} ms",
        "",
        "## Indexing / storage",
        "",
        (
            f"- Indexing time by file type: "
            f"code={indexing['indexing_time_ms_by_file_type']['code']} ms, "
            f"document={indexing['indexing_time_ms_by_file_type']['document']} ms"
        ),
        f"- Full index time (mixed project): {indexing['full_index_time_ms']} ms",
        f"- Re-index time (no changes): {indexing['reindex_time_ms_unchanged']} ms",
        f"- Generated chunk count: {indexing['generated_chunk_count']['total']} "
        f"(entities={indexing['generated_chunk_count']['code_entities']}, "
        f"document_sections={indexing['generated_chunk_count']['document_sections']})",
        f"- Vector count: {indexing['vector_count']}",
        f"- Knowledge DB size: {indexing['knowledge_db_size_bytes']} bytes",
        f"- Vector index size: {indexing['vector_index_size_bytes']} bytes",
        "",
        f"> {indexing['note']}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RagMonk search quality baseline report")
    parser.add_argument(
        "--size", default=DEFAULT_CORPUS_SIZE, choices=sorted(bench_corpus.CORPUS_SIZES)
    )
    parser.add_argument("--repeats", type=int, default=DEFAULT_LATENCY_REPEATS)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="directory to write baseline_report.json/.md into",
    )
    args = parser.parse_args(argv)

    report = generate_report(corpus_size=args.size, latency_repeats=args.repeats)

    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "baseline_report.json"
    md_path = args.out / "baseline_report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(format_summary(report), encoding="utf-8")

    print(format_summary(report))
    print(f"\nWrote {json_path}\nWrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
