"""Indexing optimization plan V2, Phase P5: per-stage wall-clock
durations (``IndexRunResult.timings`` / ``StageTimings``) and the
structured ``stage_timings`` DEBUG log event ``indexing/runner.py``'s
``run_source_pass`` emits, carrying the real trigger reason plus every
duration.

The log-event tests read the real JSON log file
(``telemetry/logging.py``'s ``configure_logging`` writes one per
``AppContext.bootstrap()`` call) rather than using pytest's ``caplog``:
the "ragmonk" logger sets ``propagate = False`` (deliberately, so this
project's own JSON file handler is the sole destination, not whatever a
host application's root logger does with it), which means records never
reach ``caplog``'s root-logger-attached handler. Reading the actual file
this project's own users would inspect is also a more faithful test of
the real "inspectable via logs" acceptance criterion than bypassing it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ragmonk.core import paths
from ragmonk.core.config import RagMonkConfig
from ragmonk.core.lifecycle import AppContext
from ragmonk.indexing.coordinator import IndexCoordinator, ScanRequest
from ragmonk.indexing.runner import build_processor_registry, run_source_pass
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect


def _read_stage_timings_events(home: Path) -> list[dict]:
    log_path = paths.logs_dir(home) / "ragmonk.log"
    if not log_path.exists():
        return []
    events = []
    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "stage_timings":
            events.append(record)
    return events


def test_full_scan_populates_scan_classify_hash_and_process_timings(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    (root / "b.py").write_text("y = 2\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())
        result = coord.run()

        assert result.indexed == 2
        # Every stage a full, non-targeted pass actually goes through
        # recorded a non-negative duration -- "no expensive tracing" does
        # not mean "no timing at all"; a warm/instant pass can still
        # legitimately measure 0.0s on a fast enough clock, so this only
        # asserts non-negativity and that hashing actually happened (two
        # new files, no precomputed hash available for either).
        assert result.timings.scan_seconds >= 0.0
        assert result.timings.classify_seconds >= 0.0
        assert result.timings.hash_seconds >= 0.0
        assert result.timings.hash_calls == 2
        assert result.timings.process_seconds >= 0.0
        # Not populated by IndexCoordinator itself -- runner.py's own
        # stages (linking/embedding/ann_sync), untouched by a bare
        # coordinator.run() call.
        assert result.timings.linking_seconds == 0.0
        assert result.timings.embedding_seconds == 0.0
        assert result.timings.ann_sync_seconds == 0.0
    finally:
        conn.close()


def test_a_warm_run_has_zero_hash_calls_and_still_records_stage_durations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())
        coord.run()

        warm = coord.run()
        assert warm.indexed == 0
        assert warm.unchanged == 1
        # stat-only fast path (Phase P3's precedent) -- no re-hash needed
        # since size/mtime already matched.
        assert warm.timings.hash_calls == 0
        assert warm.timings.scan_seconds >= 0.0
        assert warm.timings.classify_seconds >= 0.0
    finally:
        conn.close()


def test_targeted_pass_also_populates_scan_and_classify_timings(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    watched = root / "watched.py"
    watched.write_text("x = 1\n")

    conn = connect(tmp_path / "knowledge.db")
    try:
        apply_migrations(conn, "knowledge")
        coord = IndexCoordinator(conn, "s1", str(root), [], [], RagMonkConfig())
        result = coord.run(changed_paths=frozenset({str(watched.resolve())}))

        assert result.targeted is True
        assert result.new == 1
        assert result.timings.scan_seconds >= 0.0
        assert result.timings.classify_seconds >= 0.0
        assert result.timings.hash_calls == 1
        assert result.timings.process_seconds >= 0.0
    finally:
        conn.close()


def test_run_source_pass_emits_a_debug_stage_timings_event_with_trigger_reason(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """The required daemon-traceability check: a pass driven by a real
    ``ScanRequest`` reason must be traceable back to it via the emitted
    telemetry, and a slow stage must be identifiable from the recorded
    per-stage durations without a profiler.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")

    with AppContext.bootstrap(cli_overrides={"runtime": {"log_level": "debug"}}) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))
        processors = build_processor_registry(ctx.config)

        scan_request = ScanRequest(
            source_id=source.id,
            reason="local_watcher",
            full=False,
            changed_paths=frozenset({str((source_dir / "a.py").resolve())}),
        )
        pass_result = run_source_pass(ctx, source, processors, scan_request=scan_request)

        assert pass_result.result.indexed == 1

        events = _read_stage_timings_events(ctx.home)
        assert len(events) == 1
        record = events[0]
        assert record["level"] == "DEBUG"
        assert record["trigger_reason"] == "local_watcher"
        assert record["targeted"] is True
        assert record["scan_seconds"] >= 0.0
        assert record["classify_seconds"] >= 0.0
        assert record["hash_seconds"] >= 0.0
        assert record["process_seconds"] >= 0.0
        assert record["linking_seconds"] >= 0.0
        assert record["embedding_seconds"] >= 0.0
        assert record["ann_sync_seconds"] >= 0.0
        assert record["code_extraction_workers"] == ctx.config.indexing.code_extraction_workers
        assert (
            record["document_extraction_workers"] == ctx.config.indexing.document_extraction_workers
        )
        assert record["embedding_cache_reused"] == 0  # search.semantic off by default
        assert record["indexed"] == 1


def test_stage_timings_event_is_suppressed_at_the_default_info_level(
    ragmonk_home: Path, tmp_path: Path
) -> None:
    """ "Off by default at normal logging levels" -- the one existing
    mechanism this codebase already has for this (a logger level check,
    ``telemetry/logging.py``'s ``configure_logging``/``log_event``) is
    what this phase reuses, not a new config flag. At the default (info)
    log level, no stage_timings record should reach the log file at all.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")

    with AppContext.bootstrap() as ctx:  # default runtime.log_level: "info"
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))
        processors = build_processor_registry(ctx.config)

        run_source_pass(ctx, source, processors)

        assert _read_stage_timings_events(ctx.home) == []


def test_manual_cli_call_reports_manual_trigger_reason(ragmonk_home: Path, tmp_path: Path) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("x = 1\n")

    with AppContext.bootstrap(cli_overrides={"runtime": {"log_level": "debug"}}) as ctx:
        registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
        source = registry.add(str(source_dir))
        processors = build_processor_registry(ctx.config)

        # No scan_request at all -- exactly cli/index.py's own call.
        run_source_pass(ctx, source, processors)

        events = _read_stage_timings_events(ctx.home)
        assert len(events) == 1
        assert events[0]["trigger_reason"] == "manual"
