"""Metric collection for one benchmark scenario run: wall time, hashing
work, resource usage and the correctness counts from
``IndexRunResult``, plus environment fingerprinting so a baseline JSON
is comparable across machines/runs.
"""

from __future__ import annotations

import contextlib
import platform
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ragmonk.sources import fingerprint as fingerprint_module

try:
    # POSIX-only (Linux/macOS) -- unavailable on Windows. Peak
    # RSS/CPU-time reporting degrades to 0.0 there rather than making
    # this whole benchmark suite unusable on a Windows dev machine or CI
    # runner; wall time (the primary metric everything else compares
    # against) is unaffected either way.
    import resource

    _HAS_RESOURCE = True
except ImportError:  # pragma: no cover - exercised only on Windows
    resource = None  # type: ignore[assignment]
    _HAS_RESOURCE = False

# Plan finding F4: the coordinator, the document pipeline and the PDF
# adapter each did ``from ragmonk.sources.fingerprint import hash_file``
# at import time, binding their own module-level name -- patching only
# ``fingerprint_module.hash_file`` would silently miss every one of
# those calls. Every current call site is patched here explicitly so
# the counter is accurate before and after P3 changes this.
_PATCH_TARGETS = (
    "ragmonk.indexing.coordinator",
    "ragmonk.documents.pipeline",
    "ragmonk.documents.docling_adapter",
)


@dataclass
class HashCounters:
    """Counts and times every ``hash_file`` call made during a
    scenario, regardless of which module invoked it -- the coordinator,
    the document pipeline and the PDF adapter each call it independently
    today (see plan finding F4), so this is the one place the resulting
    duplication becomes visible as a number rather than an assumption.
    """

    calls: int = 0
    total_seconds: float = 0.0
    bytes_hashed: int = 0


@contextmanager
def count_hash_calls() -> Iterator[HashCounters]:
    import importlib

    counters = HashCounters()
    original = fingerprint_module.hash_file

    def _counting_hash_file(path: Path, algorithm: str = "sha256") -> str:
        started = time.perf_counter()
        try:
            return original(path, algorithm)
        finally:
            counters.calls += 1
            counters.total_seconds += time.perf_counter() - started
            with contextlib.suppress(OSError):
                counters.bytes_hashed += path.stat().st_size

    patched_modules = []
    for module_name in _PATCH_TARGETS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        if getattr(module, "hash_file", None) is original:
            module.hash_file = _counting_hash_file  # type: ignore[attr-defined]
            patched_modules.append(module)
    fingerprint_module.hash_file = _counting_hash_file
    try:
        yield counters
    finally:
        fingerprint_module.hash_file = original
        for module in patched_modules:
            module.hash_file = original  # type: ignore[attr-defined]


@dataclass
class ScenarioMetrics:
    scenario: str
    corpus_tier: str
    wall_time_s: float
    scanned: int
    new: int
    changed: int
    unchanged: int
    deleted: int
    moved: int
    indexed: int
    skipped_limit: int
    failed: int
    hash_calls: int
    hash_seconds: float
    hash_bytes: int
    cpu_user_s: float
    cpu_sys_s: float
    peak_rss_mb: float
    files_per_second: float = field(init=False)

    def __post_init__(self) -> None:
        self.files_per_second = (self.scanned / self.wall_time_s) if self.wall_time_s > 0 else 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@contextmanager
def measure_resources() -> Iterator[dict]:
    """Yields a dict populated (on exit) with wall time, CPU time and
    peak RSS for the enclosed block.
    """
    usage: dict = {}
    start_wall = time.perf_counter()
    start_rusage = resource.getrusage(resource.RUSAGE_SELF) if _HAS_RESOURCE else None
    try:
        yield usage
    finally:
        end_wall = time.perf_counter()
        usage["wall_time_s"] = end_wall - start_wall
        if _HAS_RESOURCE and start_rusage is not None:
            end_rusage = resource.getrusage(resource.RUSAGE_SELF)
            usage["cpu_user_s"] = end_rusage.ru_utime - start_rusage.ru_utime
            usage["cpu_sys_s"] = end_rusage.ru_stime - start_rusage.ru_stime
            # ru_maxrss is KB on Linux, bytes on macOS -- normalize to MB
            # assuming Linux (the only platform this benchmark targets
            # for now; documented in the baseline JSON's environment
            # block).
            divisor = 1024.0 if sys.platform != "darwin" else (1024.0 * 1024.0)
            usage["peak_rss_mb"] = end_rusage.ru_maxrss / divisor
        else:
            usage["cpu_user_s"] = 0.0
            usage["cpu_sys_s"] = 0.0
            usage["peak_rss_mb"] = 0.0


def environment_info() -> dict:
    try:
        import ragmonk

        version = getattr(ragmonk, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        version = "unknown"
    docling_available = False
    try:
        import docling  # noqa: F401

        docling_available = True
    except ImportError:
        pass
    return {
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": __import__("os").cpu_count(),
        "ragmonk_version": version,
        "docling_available": docling_available,
    }
