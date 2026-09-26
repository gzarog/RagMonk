"""CLI entry point for the real-server indexing benchmark.

    python -m benchmarks.server_indexing --engine opensearch \\
        --url http://localhost:9200 --num-files 5000 --out /tmp/os_bench.json

    python -m benchmarks.server_indexing --engine elasticsearch \\
        --url http://localhost:9200 --num-files 5000 --out /tmp/es_bench.json

See ``docs/indexing_benchmarks.md`` for the full option/metric reference.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
from pathlib import Path

from benchmarks.server_indexing.corpus import apply_incremental_changes, generate_corpus
from benchmarks.server_indexing.metrics import BenchmarkReport
from benchmarks.server_indexing.runner import (
    build_incremental_metrics,
    build_server_config,
    measure_query_latency,
    representative_queries,
    run_indexing_pass,
)


def _environment_info() -> dict[str, object]:
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }


def _reachable(engine: str, url: str) -> bool:
    try:
        from ragmonk.backends.validation import validate_server_config
        from ragmonk.core.config import ServerStorageConfig

        validate_server_config(
            ServerStorageConfig(engine=engine, url=url, verify_tls=False)  # type: ignore[arg-type]
        )
        return True
    except Exception:
        return False


def run_benchmark(
    *,
    engine: str,
    url: str,
    num_files: int,
    index_prefix: str | None,
    semantic: bool,
    query_repeats: int,
    incremental: bool,
    harness_smoke_test: bool,
) -> BenchmarkReport:
    real_cluster = bool(url) and _reachable(engine, url) and not harness_smoke_test

    with tempfile.TemporaryDirectory(prefix="ragmonk-server-bench-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        source_path = tmp_path / "corpus"
        home.mkdir(parents=True, exist_ok=True)

        files = generate_corpus(source_path, num_files)
        config = build_server_config(engine, url, index_prefix=index_prefix, semantic=semantic)

        cold_metrics, ctx, registry = run_indexing_pass(
            home=home, source_path=source_path, config=config, scenario="cold_index"
        )

        queries = representative_queries(num_files)
        lexical_stats = measure_query_latency(
            ctx, queries, label="lexical", repeats=query_repeats
        )
        hybrid_stats = None
        if semantic:
            try:
                hybrid_stats = measure_query_latency(
                    ctx, queries, label="hybrid", repeats=query_repeats, hybrid=True
                )
            except Exception as exc:  # pragma: no cover - depends on optional deps
                hybrid_stats = None
                print(f"warning: hybrid/semantic query benchmark failed: {exc}", file=sys.stderr)

        incremental_report = None
        if incremental:
            changes = apply_incremental_changes(source_path, files)
            incr_metrics, _ctx, _registry = run_indexing_pass(
                home=home,
                source_path=source_path,
                config=config,
                scenario="incremental",
                ctx=ctx,
                registry=registry,
            )
            incremental_report = build_incremental_metrics(
                corpus_size=len(files), changes=changes, index_metrics=incr_metrics
            ).as_dict()

        ctx.close()

    report = BenchmarkReport(
        label=f"server_indexing_{engine}",
        engine=engine,
        real_cluster=real_cluster,
        harness_smoke_test=harness_smoke_test or not real_cluster,
        environment=_environment_info(),
        cold_index=cold_metrics.as_dict(),
        incremental=incremental_report,
        lexical_latency=lexical_stats.as_dict() if lexical_stats.samples else None,
        hybrid_latency=hybrid_stats.as_dict() if hybrid_stats and hybrid_stats.samples else None,
    )
    if report.harness_smoke_test:
        report.notes.append(
            "HARNESS SMOKE TEST -- not a real cluster run. Either no reachable "
            f"{engine} cluster was found at the given --url, or --harness-smoke-test "
            "was explicitly requested. These numbers do not satisfy a real-cluster "
            "benchmark requirement; they only demonstrate the benchmark harness "
            "itself works end-to-end."
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["opensearch", "elasticsearch"], required=True)
    parser.add_argument("--url", default="", help="Base URL of a running cluster, e.g. http://localhost:9200")
    parser.add_argument("--num-files", type=int, default=2000)
    parser.add_argument("--index-prefix", default=None)
    parser.add_argument(
        "--semantic", action="store_true", help="Also measure hybrid/semantic query latency"
    )
    parser.add_argument("--query-repeats", type=int, default=3)
    parser.add_argument(
        "--no-incremental", action="store_true", help="Skip the incremental-update phase"
    )
    parser.add_argument(
        "--harness-smoke-test",
        action="store_true",
        help="Force-label this run as a harness smoke test regardless of cluster reachability "
        "(use for a small, fast sanity run against a fake/in-memory backend path).",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write JSON report to this path")
    args = parser.parse_args(argv)

    report = run_benchmark(
        engine=args.engine,
        url=args.url,
        num_files=args.num_files,
        index_prefix=args.index_prefix,
        semantic=args.semantic,
        query_repeats=args.query_repeats,
        incremental=not args.no_incremental,
        harness_smoke_test=args.harness_smoke_test,
    )

    payload = json.dumps(report.as_dict(), indent=2, sort_keys=False)
    if args.out:
        args.out.write_text(payload + "\n")
        print(f"wrote {args.out}")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
