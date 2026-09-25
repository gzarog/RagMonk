"""Storage backend abstraction plan, Phase 8: the daemon (``service/
daemon.py``'s ``Daemon.start``) must check server-mode backend health
before fully starting, and fail startup clearly (never silently start
anyway, never silently fall back to local) when the backend is
unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.service.daemon import Daemon


class _FakeBackend:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy
        self.health_calls = 0

    def health(self) -> bool:
        self.health_calls += 1
        return self._healthy

    def close(self) -> None:
        pass


def _server_ctx(ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppContext:
    monkeypatch.chdir(tmp_path)
    ctx = AppContext.bootstrap()
    ctx.config.storage.mode = "server"
    ctx.config.storage.server.engine = "opensearch"
    ctx.config.storage.server.url = "https://user:pass@opensearch.internal:9200"
    return ctx


def test_daemon_start_fails_clean_when_server_backend_unreachable(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _server_ctx(ragmonk_home, tmp_path, monkeypatch)
    try:
        fake = _FakeBackend(healthy=False)
        ctx._server_backend = fake  # type: ignore[assignment]
        daemon = Daemon(ctx)

        with pytest.raises(UsageError) as exc_info:
            daemon.start()

        assert fake.health_calls == 1
        message = str(exc_info.value)
        assert "unreachable" in message.lower()
        # Credentials embedded in the configured URL must never leak into
        # the raised error message.
        assert "user:pass" not in message
        assert "opensearch.internal" in message
        # Never actually started (no worker/reconciliation threads).
        assert daemon._worker_thread is None
        assert daemon._reconciliation_thread is None
    finally:
        ctx.close()


def test_daemon_start_proceeds_when_server_backend_healthy(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _server_ctx(ragmonk_home, tmp_path, monkeypatch)
    try:
        fake = _FakeBackend(healthy=True)
        ctx._server_backend = fake  # type: ignore[assignment]
        daemon = Daemon(ctx)

        daemon.start()
        try:
            assert fake.health_calls == 1
            assert daemon._worker_thread is not None
        finally:
            daemon.stop()
    finally:
        ctx.close()


def test_daemon_start_skips_health_check_in_local_mode(
    ragmonk_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = AppContext.bootstrap()
    try:
        assert ctx.config.storage.mode == "local"
        daemon = Daemon(ctx)
        daemon.start()
        try:
            assert daemon._worker_thread is not None
        finally:
            daemon.stop()
    finally:
        ctx.close()
