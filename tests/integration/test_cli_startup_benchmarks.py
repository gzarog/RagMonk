"""``pytest -m cli_startup_benchmark`` (CLI performance improvement plan,
Phase 1): measures subprocess wall-clock startup for the lightweight
commands and prints a latency report. Excluded from the default suite
(see ``pyproject.toml``'s ``addopts``) since its millisecond numbers are
only meaningful on real, unshared hardware -- a busy/virtualized CI
runner missing a target is not a regression signal, the same rationale as
``benchmark_search`` (see ``tests/integration/test_search_benchmarks.py``).

This test asserts the benchmark *runs correctly* (every command completes
without the subprocess itself crashing) rather than hard-gating on
``targets.py``'s warm-start budgets. The actual always-on regression guard
against startup slowing back down is architectural, not timing-based --
see ``tests/unit/test_cli_startup_imports.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.cli_startup import runner
from benchmarks.cli_startup.targets import COMMANDS


@pytest.mark.cli_startup_benchmark
def test_startup_benchmark_runs_every_command(tmp_path: Path) -> None:
    results = runner.run_all(home=tmp_path, repeats=3)

    report = runner.format_report(results)
    print("\n" + report)

    assert len(results) == len(COMMANDS)
    for result in results:
        assert result.cold_ms > 0
        assert result.p95_ms >= result.p50_ms >= 0

    missed = [r for r in results if not r.meets_target]
    if missed:
        names = ", ".join(f"{r.label} (p50={r.p50_ms:.1f}ms)" for r in missed)
        print(f"\nNote: missed startup targets on this hardware: {names}")
