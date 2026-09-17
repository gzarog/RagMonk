"""``python -m benchmarks.search --size small [--strict] [--repeats 20]``

Standalone runner for the search performance benchmark suite (blueprint
section 35). Generates a synthetic corpus of the requested size, runs
the eight benchmark query categories against it, and prints a latency
report. ``--strict`` exits non-zero if any category misses its
blueprint-section-36 target -- meant for a real developer machine, not
CI (see ``targets.py``'s module docstring for why).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from benchmarks.search import corpus, queries, runner
from benchmarks.search.fake_embedder import fake_embed_texts

from ragmonk.retrieval import embedder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RagMonk search performance benchmark")
    parser.add_argument(
        "--size",
        choices=sorted(corpus.CORPUS_SIZES),
        default="small",
        help="corpus size to generate",
    )
    parser.add_argument("--repeats", type=int, default=runner.DEFAULT_REPEATS)
    parser.add_argument("--limit", type=int, default=20, help="search result limit per query")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="RagMonk home directory (default: a temp directory)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit non-zero if any category misses its target"
    )
    args = parser.parse_args(argv)

    home = args.home or Path(tempfile.mkdtemp(prefix="ragmonk-benchmark-"))
    print(f"Generating {args.size!r} corpus under {home} ...", file=sys.stderr)
    generated = corpus.generate(home, size_name=args.size, seed=args.seed)
    # Query-time embedding must never hit the real model/network here --
    # see fake_embedder.py's module docstring. Restored on the way out
    # so this stays a local effect of running the benchmark script.
    real_embed_texts = embedder.embed_texts
    embedder.embed_texts = fake_embed_texts
    try:
        query_set = queries.build_queries(generated)
        results = runner.run_benchmark(generated, query_set, limit=args.limit, repeats=args.repeats)
        print(runner.format_report(generated, results))
        if args.strict and any(not r.meets_target for r in results):
            print("\nOne or more categories missed their target (--strict).", file=sys.stderr)
            return 1
        return 0
    finally:
        embedder.embed_texts = real_embed_texts
        generated.close()


if __name__ == "__main__":
    raise SystemExit(main())
