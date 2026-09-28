"""Bounded RunLock, owner metadata and cross-process contention."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from ragmonk.core.errors import RunLockTimeoutError
from ragmonk.core.lifecycle import RunLock, inspect_lock, read_lock_owner


def _hold(path: str, ready, release) -> None:  # noqa: ANN001
    lock = RunLock(Path(path), operation="index", source_id="src_example")
    lock.acquire()
    ready.set()
    release.wait(30)
    lock.release()


def _hold_and_crash(path: str, ready) -> None:  # noqa: ANN001
    import os

    RunLock(Path(path), operation="index").acquire()
    ready.set()
    os._exit(0)


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "locks" / "index.lock"


def test_uncontended_acquire_release_and_metadata(lock_path: Path) -> None:
    lock = RunLock(lock_path, operation="index", source_id="src_a")
    lock.acquire()
    owner = read_lock_owner(lock_path)
    assert owner is not None and owner["operation"] == "index"
    assert owner["source_id"] == "src_a"
    lock.release()
    lock.release()  # idempotent
    RunLock(lock_path).acquire(timeout_seconds=1)


def test_double_acquire_is_explicit_error(lock_path: Path) -> None:
    lock = RunLock(lock_path)
    lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            lock.acquire()
    finally:
        lock.release()


def test_metadata_is_sanitized(lock_path: Path) -> None:
    lock = RunLock(lock_path, operation="index https://user:pw@host/?token=abc", source_id="a b")
    lock.acquire()
    try:
        raw = lock_path.read_text(errors="replace")
        assert "pw@" not in raw and "/" not in json.loads(raw.strip("\x00 "))["operation"]
    finally:
        lock.release()


@pytest.mark.parametrize("garbage", [b"", b"\x00\xff{", b" [1,2]", b" " + b"x" * 10000])
def test_malformed_metadata_does_not_break_acquire(lock_path: Path, garbage: bytes) -> None:
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(garbage)
    assert read_lock_owner(lock_path) is None
    lock = RunLock(lock_path)
    lock.acquire(timeout_seconds=1)
    lock.release()


def test_contention_times_out_with_owner_info(lock_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(lock_path), ready, release))
    proc.start()
    try:
        assert ready.wait(30)
        assert inspect_lock(lock_path).state == "held"
        started = time.monotonic()
        with pytest.raises(RunLockTimeoutError) as info:
            RunLock(lock_path).acquire(timeout_seconds=0.5)
        assert time.monotonic() - started < 10
        assert info.value.owner_pid == proc.pid
        assert info.value.owner_source_id == "src_example"
        assert "PID" in str(info.value)
    finally:
        release.set()
        proc.join(30)
    assert inspect_lock(lock_path).state == "free"
    RunLock(lock_path).acquire(timeout_seconds=1)  # acquirable after release


def test_lock_acquirable_after_owner_crash(lock_path: Path) -> None:
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold_and_crash, args=(str(lock_path), ready))
    proc.start()
    assert ready.wait(30)
    proc.join(30)
    lock = RunLock(lock_path)
    lock.acquire(timeout_seconds=2)  # stale metadata replaced
    assert read_lock_owner(lock_path)["pid"] != proc.pid  # type: ignore[index]
    lock.release()


def test_failed_acquire_closes_handle(lock_path: Path) -> None:
    holder = RunLock(lock_path)
    holder.acquire()
    try:
        # same-process second handle: flock conflicts across open file descriptions
        contender = RunLock(lock_path)
        with pytest.raises(RunLockTimeoutError):
            contender.acquire(timeout_seconds=0.1)
        assert contender._handle is None
    finally:
        holder.release()
