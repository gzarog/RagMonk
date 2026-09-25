"""Reproducible indexing-performance benchmarks (indexing optimization
plan, Phase P0). Generates deterministic synthetic corpora on disk and
drives the real indexing entry point (``indexing.runner.run_source_pass``)
against them, recording wall time, stage timings, I/O counters and
correctness counts before and after later phases' changes.

Run with ``python -m benchmarks.indexing --help``.
"""

from __future__ import annotations
