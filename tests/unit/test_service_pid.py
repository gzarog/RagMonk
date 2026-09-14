"""``service.pid``: PID-file read/write/removal and liveness checks --
the bookkeeping a *separate* CLI invocation uses to find a running
daemon, independent of any real subprocess (see CONTRIBUTING.md for why
real ``daemon start``/``stop`` process tests live behind their own
marker instead).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ragpilot.service import pid


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    info = pid.write_pid_file(tmp_path, 12345)
    assert info.pid == 12345

    read_back = pid.read_pid_file(tmp_path)
    assert read_back is not None
    assert read_back.pid == 12345
    assert read_back.started_at == info.started_at


def test_read_missing_pid_file_returns_none(tmp_path: Path) -> None:
    assert pid.read_pid_file(tmp_path) is None


def test_read_corrupt_pid_file_returns_none_not_raises(tmp_path: Path) -> None:
    path = pid.paths.daemon_pid_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert pid.read_pid_file(tmp_path) is None


def test_remove_pid_file_is_idempotent(tmp_path: Path) -> None:
    pid.write_pid_file(tmp_path, 1)
    pid.remove_pid_file(tmp_path)
    assert pid.read_pid_file(tmp_path) is None
    pid.remove_pid_file(tmp_path)  # no error on a second removal


def test_is_process_alive_true_for_self() -> None:
    assert pid.is_process_alive(os.getpid()) is True


def test_is_process_alive_false_for_an_unused_pid() -> None:
    # A PID astronomically unlikely to be in use in this sandbox.
    assert pid.is_process_alive(2**30) is False


def test_running_daemon_is_none_for_a_dead_pid(tmp_path: Path) -> None:
    pid.write_pid_file(tmp_path, 2**30)
    assert pid.running_daemon(tmp_path) is None


def test_running_daemon_reflects_a_live_pid(tmp_path: Path) -> None:
    pid.write_pid_file(tmp_path, os.getpid())
    info = pid.running_daemon(tmp_path)
    assert info is not None
    assert info.pid == os.getpid()


class TestStopAndWait:
    """Shared by ``ops/restore.py`` (before swapping in a backup) and
    ``ops/uninstall.py`` (before purging RAGPILOT_HOME) -- signal
    delivery and process death are mocked out here (a real daemon
    subprocess is exercised separately, see the ``daemon_subprocess``
    marker in CONTRIBUTING.md).
    """

    def test_returns_false_when_no_daemon_running(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []
        monkeypatch.setattr(pid, "signal_stop", lambda p: calls.append(p))

        assert pid.stop_and_wait(tmp_path) is False
        assert calls == []

    def test_stops_and_removes_pid_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid.write_pid_file(tmp_path, 4242)
        signalled: list[int] = []
        monkeypatch.setattr(pid, "signal_stop", lambda p: signalled.append(p))
        # Alive on the first check (the signal was just sent), dead on
        # every check after -- simulates a process that stops promptly.
        alive = iter([True, False])
        monkeypatch.setattr(pid, "is_process_alive", lambda p: next(alive, False))

        result = pid.stop_and_wait(tmp_path, timeout_seconds=5)

        assert result is True
        assert signalled == [4242]
        assert pid.read_pid_file(tmp_path) is None

    def test_raises_error_cls_when_process_never_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid.write_pid_file(tmp_path, 4242)
        monkeypatch.setattr(pid, "signal_stop", lambda p: None)
        monkeypatch.setattr(pid, "is_process_alive", lambda p: True)

        with pytest.raises(RuntimeError, match="did not stop"):
            pid.stop_and_wait(tmp_path, timeout_seconds=0.05, poll_interval_seconds=0.01)

    def test_uses_the_given_error_cls_and_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _CustomError(Exception):
            pass

        pid.write_pid_file(tmp_path, 4242)
        monkeypatch.setattr(pid, "signal_stop", lambda p: None)
        monkeypatch.setattr(pid, "is_process_alive", lambda p: True)

        with pytest.raises(_CustomError, match="refusing to purge data"):
            pid.stop_and_wait(
                tmp_path,
                timeout_seconds=0.05,
                poll_interval_seconds=0.01,
                action="purge data",
                error_cls=_CustomError,
            )
