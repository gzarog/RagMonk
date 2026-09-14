"""Startup latency targets (CLI performance improvement plan, Phase 1).

Warm-start budgets for the lightweight commands that must never pay the
cost of loading Docling/torch/transformers/mcp/AI-SDK/usearch. Meant for a
real, unshared developer machine -- a busy/virtualized CI runner is
neither, so these are reported as diagnostic pass/fail rather than a hard
gate; see ``benchmarks/search/targets.py`` for the same rationale applied
to search latency.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StartupTarget:
    warm_ms: float


# Command label -> the CLI args that invoke it, and its warm-start budget.
COMMANDS: dict[str, list[str]] = {
    "version": ["version"],
    "--help": ["--help"],
    "config --help": ["config", "--help"],
    "status": ["status"],
    "search --help": ["search", "--help"],
}

TARGETS: dict[str, StartupTarget] = {
    "version": StartupTarget(warm_ms=200),
    "--help": StartupTarget(warm_ms=300),
    "config --help": StartupTarget(warm_ms=300),
    "status": StartupTarget(warm_ms=500),
    "search --help": StartupTarget(warm_ms=300),
}
