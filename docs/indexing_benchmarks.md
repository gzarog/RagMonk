# Indexing performance benchmarks

`benchmarks/indexing/` is a reproducible, fully-offline benchmark suite
for RagMonk's indexing pipeline, built as Phase P0 of the indexing
performance optimization plan (`ragmonk-indexing-performance-v1`, see
`CHANGELOG.md`'s Unreleased section). It exists so every later
optimization phase is validated against real, measured before/after
numbers instead of an assumed speedup.

## What it measures

For a generated synthetic corpus, the suite runs the exact production
indexing entry point (`ragmonk.indexing.runner.run_source_pass` — the
same code path `ragmonk index` and the daemon use) through a fixed
sequence of realistic scenarios:

| Scenario             | What it simulates                                  |
| --------------------- | --------------------------------------------------- |
| `cold_index`           | First-ever index of a brand new source               |
| `warm_unchanged`       | Re-running with nothing changed                      |
| `single_edit`          | One file edited                                      |
| `one_percent_change`   | ~1% of files edited                                  |
| `burst_change`         | ~5% of files edited (a burst of activity)            |
| `rename`               | One file moved/renamed                               |
| `delete`               | A small batch of files deleted                       |

For each scenario it records: wall time, files/sec, scan result counts
(new/changed/unchanged/deleted/moved/indexed/failed), the number and
total duration/bytes of `hash_file` calls (across every module that
calls it today — the coordinator, the document pipeline and the PDF
adapter independently, per finding F4), CPU time, peak RSS, and
correctness counts (entities, document sections) from the resulting
SQLite database.

Everything runs against **generated, synthetic content only** — code
files are small deterministic Python modules; document files are
repeated copies of the repository's own tiny, already-committed
`tests/fixtures/documents/*.{txt,md,html,csv}` samples. No confidential
or real-world data is ever generated, downloaded or committed.

## Running it

```bash
# from a venv with the project installed (`uv pip install -e ".[dev]"`)
python -m benchmarks.indexing --tier small
python -m benchmarks.indexing --tier medium --out /tmp/my_run.json
python -m benchmarks.indexing --tier large    # 10k files, several minutes
python -m benchmarks.indexing --tier scale    # 200k files, optional, slow
```

Tiers (`--tier`): `tiny` (50 files), `small` (500), `medium` (2,000),
`large` (10,000), `scale` (200,000, opt-in — not run by default or in
CI due to disk/time cost).

The run is fully offline: no network access, no cloud API keys. If
Docling/torch are not installed in the current environment, document
indexing is automatically disabled for the run
(`RAGMONK_DOCUMENTS__ENABLED=false`) and this is recorded in the output
JSON's `environment.docling_available` field rather than silently
skipped.

Output is a machine-readable JSON file (default
`benchmarks/indexing/baseline_<tier>.json`) with one record per
scenario plus an environment block (Python version, platform, CPU
count, RagMonk version, Docling availability) so results are
comparable across runs and machines.

## Recorded baselines

`benchmarks/indexing/baseline_small.json` and `baseline_medium.json` in
this directory are the **measured baseline** against the pre-P1
implementation (commit `d8a5fad`), captured on the CI/dev container this
work was done in. Re-run the suite yourself before trusting these
numbers on different hardware — they are not portable performance
guarantees, only a reproducible reference point for regression
comparison.

## Extending it

`fixtures.py` holds the corpus generator; `metrics.py` holds the
resource/hash-call instrumentation; `runner.py` drives one scenario
through the real indexing entry point. Later phases (P1–P7) should
extend this suite's scenarios rather than replace it — in particular,
P7 re-runs these exact fixtures against the optimized implementation
for the plan's required before/after report.
