"""``update/notifier.py``: reads the local cache only, prints at most once
per newly-discovered version, and respects ``updates.enabled``/``notify``.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from ragpilot import __version__
from ragpilot.core.config import UpdatesConfig
from ragpilot.update import cache, notifier
from ragpilot.update.models import UpdateCache

_NEWER = "999.0.0"


def _capturing_console() -> tuple[Console, StringIO]:
    buf = StringIO()
    return Console(file=buf, force_terminal=False, width=200), buf


def _write_cache(
    tmp_path: Path, *, latest_version: str, last_notified_version: str | None = None
) -> None:
    cache.write_cache(
        tmp_path,
        UpdateCache(
            last_checked="2026-09-11T00:00:00+00:00",
            installed_version=__version__,
            latest_version=latest_version,
            release_url="https://x",
            last_notified_version=last_notified_version,
        ),
    )


def test_no_cache_prints_nothing(tmp_path: Path) -> None:
    console, buf = _capturing_console()
    notifier.maybe_notify(tmp_path, UpdatesConfig(), console=console)
    assert buf.getvalue() == ""


def test_not_newer_prints_nothing(tmp_path: Path) -> None:
    # The installed version itself is never "newer than itself".
    _write_cache(tmp_path, latest_version=__version__)
    console, buf = _capturing_console()

    notifier.maybe_notify(tmp_path, UpdatesConfig(), console=console)

    assert buf.getvalue() == ""


def test_newer_and_unnotified_prints_and_marks_notified(tmp_path: Path) -> None:
    _write_cache(tmp_path, latest_version=_NEWER)
    console, buf = _capturing_console()

    notifier.maybe_notify(tmp_path, UpdatesConfig(), console=console)

    output = buf.getvalue()
    assert __version__ in output
    assert _NEWER in output
    assert "ragpilot update install" in output
    result = cache.read_cache(tmp_path)
    assert result is not None
    assert result.last_notified_version == _NEWER


def test_already_notified_version_prints_nothing_again(tmp_path: Path) -> None:
    _write_cache(tmp_path, latest_version=_NEWER, last_notified_version=_NEWER)
    console, buf = _capturing_console()

    notifier.maybe_notify(tmp_path, UpdatesConfig(), console=console)

    assert buf.getvalue() == ""


def test_disabled_prints_nothing(tmp_path: Path) -> None:
    _write_cache(tmp_path, latest_version=_NEWER)
    console, buf = _capturing_console()

    notifier.maybe_notify(tmp_path, UpdatesConfig(enabled=False), console=console)

    assert buf.getvalue() == ""


def test_notify_off_prints_nothing(tmp_path: Path) -> None:
    _write_cache(tmp_path, latest_version=_NEWER)
    console, buf = _capturing_console()

    notifier.maybe_notify(tmp_path, UpdatesConfig(notify=False), console=console)

    assert buf.getvalue() == ""
