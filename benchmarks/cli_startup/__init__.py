"""Standalone CLI startup latency benchmark (CLI performance improvement
plan, Phase 1) -- a sibling of ``benchmarks/search/``, not installed with
the project (see ``pyproject.toml``'s ``pythonpath`` test setting, which
lets ``tests/`` import it without a packaging step).

Measures cold/warm wall-clock startup for the lightweight commands that
should start almost immediately: ``version``, ``--help``, ``config
--help``, ``status``, ``search --help``. Diagnostic, not a hard CI gate --
see ``targets.py`` and ``tests/integration/test_cli_startup_benchmarks.py``
for why. The actual regression guard against startup slowing back down is
architectural, not timing-based: ``tests/unit/test_cli_startup_imports.py``
asserts these commands never import Docling/torch/transformers/mcp/AI-SDK/
usearch.
"""
