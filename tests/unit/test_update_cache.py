"""``update/cache.py``: ``<RAGPILOT_HOME>/update.json`` read/write
round-trip, and the "missing/corrupt cache is not an error" contract
(``read_cache`` returns ``None`` rather than raising).
"""

from __future__ import annotations

from pathlib import Path

from ragpilot.core import paths
from ragpilot.update import cache
from ragpilot.update.models import UpdateCache


def _sample() -> UpdateCache:
    return UpdateCache(
        last_checked="2026-09-11T08:00:00+00:00",
        installed_version="0.1.7",
        latest_version="0.1.8",
        release_url="https://github.com/gzarog/Ragpilotv2/releases/tag/v0.1.8",
        last_notified_version=None,
    )


def test_read_cache_returns_none_when_file_is_missing(tmp_path: Path) -> None:
    assert cache.read_cache(tmp_path) is None


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    cache.write_cache(tmp_path, _sample())

    result = cache.read_cache(tmp_path)

    assert result == _sample()
    assert paths.update_cache_path(tmp_path).is_file()


def test_read_cache_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    path = paths.update_cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    assert cache.read_cache(tmp_path) is None


def test_read_cache_returns_none_on_missing_required_field(tmp_path: Path) -> None:
    path = paths.update_cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"installed_version": "0.1.7"}', encoding="utf-8")

    assert cache.read_cache(tmp_path) is None


def test_write_cache_creates_home_directory_if_missing(tmp_path: Path) -> None:
    home = tmp_path / "does" / "not" / "exist" / "yet"

    cache.write_cache(home, _sample())

    assert paths.update_cache_path(home).is_file()


def test_write_cache_overwrites_previous_contents(tmp_path: Path) -> None:
    cache.write_cache(tmp_path, _sample())
    newer = UpdateCache(
        last_checked="2026-09-12T08:00:00+00:00",
        installed_version="0.1.8",
        latest_version="0.1.8",
        release_url="https://github.com/gzarog/Ragpilotv2/releases/tag/v0.1.8",
    )

    cache.write_cache(tmp_path, newer)

    assert cache.read_cache(tmp_path) == newer
