"""Search performance benchmarks (blueprint section 35).

Builds synthetic corpora directly through the storage repositories
(``ragmonk.storage.repositories.*``), bypassing Tree-sitter/Docling
parsing entirely -- the point is to measure the *retrieval* layer at
scale, not indexing throughput, which already has its own coverage
elsewhere. See ``corpus.py`` for corpus generation, ``queries.py`` for
the blueprint's eight benchmark query categories, ``runner.py`` for
latency measurement, and ``targets.py`` for the blueprint's stated
performance targets (section 36).

Run standalone via ``python -m benchmarks.search --size small`` (see
that module's ``--help``), or via the fast, always-on ``small`` corpus
under ``pytest -m benchmark_search`` -- see CONTRIBUTING.md.
"""

from __future__ import annotations
