"""``python -m benchmarks.indexing --tier small [--out path.json]``

Standalone runner for the indexing performance benchmark suite
(indexing optimization plan, Phase P0). Generates a synthetic fixture
corpus, runs a fixed sequence of realistic scenarios against it (cold
index, warm/unchanged re-index, single edit, 1% change, burst, rename,
delete) and writes a machine-readable baseline JSON.

Runs fully offline: no network access and no cloud API keys are used.
When Docling/torch are not installed in the current environment,
document-kind indexing is disabled for the run (``RAGMONK_DOCUMENTS__
ENABLED=false``) and this is recorded in the output JSON's environment
block rather than silently skipped -- see ``docling_available``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.indexing import fixtures
from benchmarks.indexing.metrics import environment_info
from benchmarks.indexing.runner import run_scenario

TIER_SIZES: dict[str, int] = {
    "tiny": 50,
    "small": 500,
    "medium": 2_000,
    "large": 10_000,
    # Explicitly out of scope for a default run -- see the plan's
    # optional 200k-file scale tier. Available via --tier scale but not
    # run in CI or by default (disk/time cost documented in the report).
    "scale": 200_000,
}


def _run_tier(tier: str, n_files: int, *, keep: bool) -> list[dict]:
    docling_ok = environment_info()["docling_available"]
    if not docling_ok:
        os.environ["RAGMONK_DOCUMENTS__ENABLED"] = "false"
    # Keeps the benchmark fully offline and its output free of an
    # unrelated update-check print -- matches the P0 acceptance
    # criterion that baseline runs need no network access.
    os.environ["RAGMONK_UPDATES__ENABLED"] = "false"

    workdir = Path(tempfile.mkdtemp(prefix=f"ragmonk_bench_{tier}_"))
    home = workdir / "home"
    source_root = workdir / "source"
    source_root.mkdir(parents=True)

    code_files = fixtures.generate_code_corpus(source_root, n_files)
    doc_count = max(5, n_files // 20)
    fixtures.generate_mixed_document_corpus(source_root, doc_count)
    all_records: list[dict] = []

    def _record(scenario: str) -> None:
        metrics, correctness = run_scenario(
            home=home, source_path=source_root, scenario=scenario, corpus_tier=tier
        )
        row = metrics.to_dict()
        row["entities_total"] = correctness.entities
        row["document_sections_total"] = correctness.document_sections
        all_records.append(row)
        print(
            f"[{tier}] {scenario:14s} wall={metrics.wall_time_s:8.3f}s "
            f"scanned={metrics.scanned:7d} new={metrics.new:6d} changed={metrics.changed:6d} "
            f"unchanged={metrics.unchanged:7d} hash_calls={metrics.hash_calls:6d}"
        )

    _record("cold_index")
    _record("warm_unchanged")
    fixtures.apply_single_edit(code_files, seed=1)
    _record("single_edit")
    fixtures.apply_percent_change(code_files, 0.01, seed=2)
    _record("one_percent_change")
    fixtures.apply_percent_change(code_files, 0.05, seed=3)
    _record("burst_change")
    fixtures.apply_rename(code_files, seed=4)
    _record("rename")
    fixtures.apply_delete(code_files, max(1, n_files // 100), seed=5)
    _record("delete")

    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)

    return all_records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RagMonk indexing performance benchmark")
    parser.add_argument("--tier", choices=sorted(TIER_SIZES), default="small")
    parser.add_argument("--out", type=Path, default=None, help="output JSON path")
    parser.add_argument(
        "--keep-workdir", action="store_true", help="do not delete the generated fixture corpus"
    )
    args = parser.parse_args(argv)

    n_files = TIER_SIZES[args.tier]
    scenarios = _run_tier(args.tier, n_files, keep=args.keep_workdir)

    payload = {
        "plan_id": "ragmonk-indexing-performance-v1",
        "phase": "P0",
        "generated_at": datetime.now(UTC).isoformat(),
        "tier": args.tier,
        "n_files_requested": n_files,
        "environment": environment_info(),
        "scenarios": scenarios,
    }

    out = args.out or Path(f"benchmarks/indexing/baseline_{args.tier}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
