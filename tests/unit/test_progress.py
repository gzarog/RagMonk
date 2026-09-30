"""Status observability V1: the persisted live indexing progress snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragmonk.core import paths
from ragmonk.service import progress


def test_missing_snapshot_reads_as_unavailable(tmp_path: Path) -> None:
    assert progress.read_progress(tmp_path) is None


@pytest.mark.parametrize(
    "raw",
    ["", "{not json", "[1, 2]", '{"schema_version": 99, "running": true}', '"text"'],
)
def test_malformed_or_incompatible_snapshot_reads_as_unavailable(tmp_path: Path, raw: str) -> None:
    paths.index_progress_path(tmp_path).write_text(raw, encoding="utf-8")
    assert progress.read_progress(tmp_path) is None


def test_wrongly_typed_fields_fall_back_to_defaults(tmp_path: Path) -> None:
    paths.index_progress_path(tmp_path).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "running": "yes",
                "pid": True,
                "indexed": "12",
                "stage": "processing",
                "unknown_future_field": 1,
            }
        ),
        encoding="utf-8",
    )
    snapshot = progress.read_progress(tmp_path)
    assert snapshot is not None
    assert snapshot.running is False
    assert snapshot.pid is None
    assert snapshot.indexed == 0
    assert snapshot.stage == "processing"


def test_write_then_read_round_trip_leaves_no_tmp_files(tmp_path: Path) -> None:
    snapshot = progress.IndexProgress(running=True, pid=42, operation="index", indexed=3)
    progress.write_progress(tmp_path, snapshot)
    assert progress.read_progress(tmp_path) == snapshot
    assert [p.name for p in tmp_path.iterdir()] == ["index_progress.json"]


def test_track_records_sources_stages_counters_and_completion(tmp_path: Path) -> None:
    with progress.track(tmp_path, operation="index", source_total=2) as tracker:
        assert progress.current() is tracker
        tracker.begin_source("src_a", 1)
        tracker.scanned(5, queued=3)
        tracker.stage("processing")
        tracker.file_done("indexed")
        tracker.file_done("failed")
        tracker.file_done("retry")
        tracker.begin_source("src_b", 2)
        live = progress.read_progress(tmp_path)
        assert live is not None
        assert live.running is True
        assert live.source_id == "src_b"
        assert live.source_position == 2
        assert live.source_total == 2
        assert live.stage == "scan"

    final = progress.read_progress(tmp_path)
    assert final is not None
    assert final.running is False
    assert final.outcome == "completed"
    assert final.completed_at is not None
    assert (final.scanned, final.indexed, final.failed, final.retry) == (5, 1, 1, 1)
    assert progress.current() is not tracker


def test_track_finalizes_on_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), progress.track(tmp_path, operation="index"):
        raise RuntimeError("boom http://user:secret@host/x")
    final = progress.read_progress(tmp_path)
    assert final is not None
    assert final.running is False
    assert final.outcome == "failed"
    assert final.error is not None and "boom" in final.error
    assert "secret" not in final.error


def test_track_is_reentrant(tmp_path: Path) -> None:
    with progress.track(tmp_path, operation="rebuild") as outer:
        with progress.track(tmp_path, operation="index") as inner:
            assert inner is outer
        # The nested exit must not finalize the outer run.
        snapshot = progress.read_progress(tmp_path)
        assert snapshot is not None and snapshot.running is True


def test_counter_updates_are_coalesced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[int] = []
    real_write = progress.write_progress

    def counting_write(home: Path, snapshot: progress.IndexProgress) -> None:
        writes.append(snapshot.indexed)
        real_write(home, snapshot)

    monkeypatch.setattr(progress, "write_progress", counting_write)
    with progress.track(tmp_path, operation="index") as tracker:
        for _ in range(500):
            tracker.file_done("indexed")
    # start + finish, plus at most a handful of time-based flushes --
    # never one write per file.
    assert len(writes) < 10
    final = progress.read_progress(tmp_path)
    assert final is not None and final.indexed == 500


def test_null_tracker_outside_a_run_is_inert(tmp_path: Path) -> None:
    tracker = progress.current()
    tracker.stage("processing")
    tracker.file_done("indexed")
    tracker.finish()
    assert not paths.index_progress_path(tmp_path).exists()
