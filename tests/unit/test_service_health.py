"""``service.health``: the daemon's heartbeat snapshot, read/write
round-trip and "no snapshot yet" handling -- what ``ragmonk daemon
status``/``doctor`` read without talking to the daemon process itself.
"""

from __future__ import annotations

from pathlib import Path

from ragmonk.service import health


def test_read_missing_health_file_returns_none(tmp_path: Path) -> None:
    assert health.read_health(tmp_path) is None


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    snapshot = health.DaemonHealth(
        started_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:05:00+00:00",
        last_reconciliation_at="2026-01-01T00:04:00+00:00",
        sources=[
            health.SourceWatchStatus(
                source_id="src_1",
                path="/a/b",
                source_type="local",
                online=True,
                last_pass_at="2026-01-01T00:04:30+00:00",
            )
        ],
    )
    health.write_health(tmp_path, snapshot)

    read_back = health.read_health(tmp_path)
    assert read_back == snapshot


def test_read_corrupt_health_file_returns_none_not_raises(tmp_path: Path) -> None:
    path = health.paths.daemon_health_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert health.read_health(tmp_path) is None


def test_write_is_atomic_no_tmp_file_left_behind(tmp_path: Path) -> None:
    snapshot = health.DaemonHealth(started_at="t0", updated_at="t1")
    health.write_health(tmp_path, snapshot)
    leftover = health.paths.daemon_health_path(tmp_path).with_suffix(".json.tmp")
    assert not leftover.exists()
