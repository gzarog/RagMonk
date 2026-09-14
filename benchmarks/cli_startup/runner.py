"""Subprocess wall-clock measurement for ``benchmarks/cli_startup``.

Each command is measured as a fresh ``python -m ragpilot.cli.main ...``
process -- a real interpreter start, real module import, real Typer
dispatch -- rather than an in-process call, since in-process timing would
not capture the cost this benchmark exists to catch (import-time work
pulling in heavy optional subsystems).
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from benchmarks.cli_startup.targets import COMMANDS, TARGETS, StartupTarget

DEFAULT_REPEATS = 10


@dataclass(frozen=True)
class StartupResult:
    label: str
    cold_ms: float
    p50_ms: float
    p95_ms: float
    target: StartupTarget

    @property
    def meets_target(self) -> bool:
        return self.p50_ms <= self.target.warm_ms


def _percentile(sorted_samples: list[float], pct: float) -> float:
    if not sorted_samples:
        return 0.0
    rank = (pct / 100) * (len(sorted_samples) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_samples[lower]
    fraction = rank - lower
    return sorted_samples[lower] + (sorted_samples[upper] - sorted_samples[lower]) * fraction


def _now_ms() -> float:
    return time.perf_counter() * 1000


def _invoke(cli_args: list[str], *, env: dict[str, str]) -> float:
    started = _now_ms()
    subprocess.run(
        [sys.executable, "-m", "ragpilot.cli.main", *cli_args],
        capture_output=True,
        env=env,
        check=False,
    )
    return _now_ms() - started


def measure(
    label: str, cli_args: list[str], *, home: Path, repeats: int = DEFAULT_REPEATS
) -> StartupResult:
    """Runs ``cli_args`` once (cold) then ``repeats`` more times (warm),
    each invocation isolated to ``home`` -- see the ``ragpilot_home``
    pytest fixture's own rationale for why real ``~/.ragpilot`` must never
    be touched by a test or benchmark run.
    """
    target = TARGETS[label]
    env = {**os.environ, "RAGPILOT_HOME": str(home)}
    cold_ms = _invoke(cli_args, env=env)
    samples = sorted(_invoke(cli_args, env=env) for _ in range(repeats))
    return StartupResult(
        label=label,
        cold_ms=cold_ms,
        p50_ms=_percentile(samples, 50),
        p95_ms=_percentile(samples, 95),
        target=target,
    )


def run_all(*, home: Path, repeats: int = DEFAULT_REPEATS) -> list[StartupResult]:
    return [measure(label, args, home=home, repeats=repeats) for label, args in COMMANDS.items()]


def format_report(results: list[StartupResult]) -> str:
    header = f"{'command':<16}{'cold':>10}{'p50':>10}{'p95':>10}{'target(p50)':>14}  status"
    lines = ["CLI startup latency (subprocess wall-clock, ms):", "", header]
    for r in results:
        status = "OK" if r.meets_target else "MISS"
        lines.append(
            f"{r.label:<16}{r.cold_ms:>10.1f}{r.p50_ms:>10.1f}{r.p95_ms:>10.1f}"
            f"{r.target.warm_ms:>14.1f}  {status}"
        )
    return "\n".join(lines)
