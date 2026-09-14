"""``AppContext.bootstrap()``/``close()`` wiring to the update subsystem
(CLI performance improvement plan, Phase 4): the background-check
decision happens on bootstrap, the notification on close, both are
passed ``config.updates``, and neither ever propagates an exception --
see ``core/lifecycle.py``'s own comments for why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragpilot.core import lifecycle
from ragpilot.core.lifecycle import AppContext


def test_bootstrap_calls_maybe_launch_background_check(
    ragpilot_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        lifecycle.update_background,
        "maybe_launch_background_check",
        lambda home, config: calls.append((home, config)),
    )

    with AppContext.bootstrap() as ctx:
        assert len(calls) == 1
        assert calls[0][0] == ctx.home
        assert calls[0][1] is ctx.config.updates


def test_bootstrap_survives_background_check_raising(
    ragpilot_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(home: Path, config: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle.update_background, "maybe_launch_background_check", _raise)

    with AppContext.bootstrap() as ctx:
        assert ctx is not None  # bootstrap() itself never raised


def test_close_calls_maybe_notify(ragpilot_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        lifecycle.update_notifier,
        "maybe_notify",
        lambda home, config: calls.append((home, config)),
    )

    with AppContext.bootstrap() as ctx:
        home, config = ctx.home, ctx.config.updates

    assert calls == [(home, config)]


def test_close_survives_notify_raising(
    ragpilot_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(home: Path, config: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle.update_notifier, "maybe_notify", _raise)

    with AppContext.bootstrap() as ctx:
        pass  # exiting the `with` block calls close(), which must not raise
    assert ctx is not None
