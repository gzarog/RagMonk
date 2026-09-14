"""Search *quality* benchmarking (Phase 0 of the search-quality
improvement plan): golden-query Recall@K/MRR evaluation
(``evaluator.py``) and a structured baseline report combining those
numbers with the latency/indexing/storage measurements already produced
by ``benchmarks/search`` (``report.py``).

Kept as its own top-level package, a sibling of ``benchmarks/search``
(the *speed* benchmark suite -- synthetic corpora, p50/p95 latency
targets): this package is about correctness/relevance instead, always
measured against the real, small, hand-written fixture project in
``fixture_project.py`` rather than a synthetic corpus, so its numbers
stay deterministic and meaningful in CI.
"""

from __future__ import annotations
