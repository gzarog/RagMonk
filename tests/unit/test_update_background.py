"""``update/background.py``: cache-staleness decision, whether/how a
detached checker process gets spawned (``subprocess.Popen`` itself is
never actually invoked here -- monkeypatched to a spy), and
``run_background_check``'s own network-mocked success/failure paths and
``last_notified_version`` carry-forward logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ragpilot.core.config import UpdatesConfig
from ragpilot.update import background, cache, checker
from ragpilot.update.models import ReleaseInfo, UpdateCache


def _cache_entry(*, last_checked: str, latest_version: str = "0.1.8") -> UpdateCache:
    return UpdateCache(
        last_checked=last_checked,
        installed_version="0.1.7",
        latest_version=latest_version,
        release_url="https://github.com/gzarog/Ragpilotv2/releases/tag/v0.1.8",
    )


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


class TestIsCacheStale:
    def test_none_is_stale(self) -> None:
        assert background.is_cache_stale(None, interval_hours=24) is True

    def test_recent_is_not_stale(self) -> None:
        entry = _cache_entry(last_checked=_iso(timedelta(hours=1)))
        assert background.is_cache_stale(entry, interval_hours=24) is False

    def test_old_is_stale(self) -> None:
        entry = _cache_entry(last_checked=_iso(timedelta(hours=25)))
        assert background.is_cache_stale(entry, interval_hours=24) is True

    def test_malformed_timestamp_is_stale(self) -> None:
        entry = _cache_entry(last_checked="not-a-timestamp")
        assert background.is_cache_stale(entry, interval_hours=24) is True


class TestMaybeLaunchBackgroundCheck:
    def test_disabled_never_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: calls.append(a))

        background.maybe_launch_background_check(tmp_path, UpdatesConfig(enabled=False))

        assert calls == []

    def test_fresh_cache_never_spawns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.write_cache(tmp_path, _cache_entry(last_checked=_iso(timedelta(hours=1))))
        calls: list[Any] = []
        monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: calls.append(a))

        background.maybe_launch_background_check(
            tmp_path, UpdatesConfig(enabled=True, check_interval_hours=24)
        )

        assert calls == []

    def test_missing_cache_spawns_with_expected_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))

        background.maybe_launch_background_check(tmp_path, UpdatesConfig(enabled=True))

        assert len(calls) == 1
        argv = calls[0][0][0]
        assert argv[1:3] == ["-m", "ragpilot.update.background"]
        assert argv[3] == str(tmp_path)
        assert calls[0][1]["start_new_session"] is True

    def test_stale_cache_spawns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cache.write_cache(tmp_path, _cache_entry(last_checked=_iso(timedelta(hours=48))))
        calls: list[Any] = []
        monkeypatch.setattr(background.subprocess, "Popen", lambda *a, **kw: calls.append(a))

        background.maybe_launch_background_check(
            tmp_path, UpdatesConfig(enabled=True, check_interval_hours=24)
        )

        assert len(calls) == 1

    def test_spawn_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a: object, **kw: object) -> None:
            raise OSError("no fork permission")

        monkeypatch.setattr(background.subprocess, "Popen", _raise)

        background.maybe_launch_background_check(tmp_path, UpdatesConfig(enabled=True))  # no raise


class TestRunBackgroundCheck:
    def test_success_writes_cache_with_no_prior_notification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            checker,
            "fetch_latest_release",
            lambda **_: ReleaseInfo(version="0.1.8", tag_name="v0.1.8", html_url="https://x"),
        )

        background.run_background_check(tmp_path)

        result = cache.read_cache(tmp_path)
        assert result is not None
        assert result.latest_version == "0.1.8"
        assert result.last_notified_version is None

    def test_same_version_carries_notified_marker_forward(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.write_cache(
            tmp_path,
            UpdateCache(
                last_checked=_iso(timedelta(hours=48)),
                installed_version="0.1.7",
                latest_version="0.1.8",
                release_url="https://x",
                last_notified_version="0.1.8",
            ),
        )
        monkeypatch.setattr(
            checker,
            "fetch_latest_release",
            lambda **_: ReleaseInfo(version="0.1.8", tag_name="v0.1.8", html_url="https://x"),
        )

        background.run_background_check(tmp_path)

        assert cache.read_cache(tmp_path).last_notified_version == "0.1.8"

    def test_new_version_resets_notified_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache.write_cache(
            tmp_path,
            UpdateCache(
                last_checked=_iso(timedelta(hours=48)),
                installed_version="0.1.7",
                latest_version="0.1.8",
                release_url="https://x",
                last_notified_version="0.1.8",
            ),
        )
        monkeypatch.setattr(
            checker,
            "fetch_latest_release",
            lambda **_: ReleaseInfo(version="0.1.9", tag_name="v0.1.9", html_url="https://y"),
        )

        background.run_background_check(tmp_path)

        result = cache.read_cache(tmp_path)
        assert result.latest_version == "0.1.9"
        assert result.last_notified_version is None

    def test_checker_failure_leaves_cache_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(**_: object) -> None:
            raise checker.UpdateCheckError("offline")

        monkeypatch.setattr(checker, "fetch_latest_release", _raise)

        background.run_background_check(tmp_path)  # never raises

        assert cache.read_cache(tmp_path) is None
