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

## Phase P7: before/after report

`benchmarks/indexing/after_p7_small.json` is the **measured "after"**
run, same `small` tier, same corpus generator, captured on the same
CI/dev container as `baseline_small.json` after every optimization
phase (P1–P6) plus one correctness fix P7 itself found and fixed (see
below) had landed.

| Scenario             | before: `changed` | after: `changed` | before: wall | after: wall |
| --------------------- | -----------------: | -----------------: | -------------: | ------------: |
| `cold_index`           |                  0 |                  0 |        28.4s   |       22.3s   |
| `warm_unchanged`       |                  0 |                  0 |        0.109s  |       0.071s  |
| `single_edit`          |                  1 |                  1 |        0.135s  |       0.119s  |
| `one_percent_change`   |                  6 |                  5 |        0.240s  |       0.156s  |
| `burst_change`         |                 31 |                 25 |        0.659s  |       0.302s  |
| `rename`               |             **31** |              **0** |    **0.583s**  |   **0.070s**  |
| `delete`               |             **31** |              **0** |    **0.774s**  |   **0.082s**  |

`hash_calls` moves in exact lockstep with `changed` in both runs (P3's
reuse already ensures one hash per genuinely changed file, never more)
— the story here is entirely in how many files `changed` should have
been, not in per-file hashing cost.

### The bug P7 found

Every phase before P7 validated its own change in isolation — one edit,
one rerun. Phase P7's job (this file's own `## Extending it` note,
below, always said P7 would run the full suite end-to-end) surfaced
something none of them could: **a genuinely pre-existing bug, present
in `baseline_small.json`'s own pre-P1 numbers above and therefore
predating this entire optimization plan.**

`IndexCoordinator`'s scan loop computed a changed file's new size/mtime/
content_hash correctly, but only ever persisted its `status` to the
`files` table — never the new stat. The next file to read that row
(`_process_queue`'s `files_repo.get()`) got back the *old* size/mtime/
content_hash, and `mark_indexed` dutifully wrote those stale values
straight back at the end. The practical effect: **every edited file
looked "changed" again on every subsequent `ragmonk index` run,
forever** — regardless of Phase P3's content-hash reuse or Phase P2's
targeted scanning, both of which were correctly detecting a real
mismatch against data that was itself wrong. The `rename`/`delete`
scenarios' `changed: 31` above (pre-fix) is this bug in action: files
"changed" by an earlier scenario in the same run never actually
cleared.

Fixed in `files_repo.update_status()` (now accepts optional `size`/
`mtime`/`content_hash`, `COALESCE`d against the stored value like
`mark_indexed`'s own version-stamp params) and its three call sites in
`indexing/coordinator.py` (the full-scan path and both targeted-scan
branches). See `tests/unit/test_index_coordinator_changed_file_stat_
persistence.py` and `tests/integration/test_indexing_optimization_
regression.py` for the regression coverage, and this file's own git
history for the full before/after story.

## Extending it

`fixtures.py` holds the corpus generator; `metrics.py` holds the
resource/hash-call instrumentation; `runner.py` drives one scenario
through the real indexing entry point. Later phases (P1–P7) should
extend this suite's scenarios rather than replace it — in particular,
P7 re-runs these exact fixtures against the optimized implementation
for the plan's required before/after report.
