"""Storage backend abstraction plan, Phase 8: ``ragmonk status``/
``collect_status`` reports the configured backend type (local vs
server/opensearch vs server/elasticsearch) and, in server mode, real
counts from ``ctx.backend().count_stats()``. Local mode's own output
(the ``sources``/``totals`` blocks) stays exactly as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ragmonk.backends.models import BackendStats
from ragmonk.cli.main import app
from ragmonk.core.lifecycle import AppContext
from ragmonk.service.status_service import _backend_kind, collect_status


class _StatsBackend:
    def __init__(self, stats: BackendStats) -> None:
        self._stats = stats

    def count_stats(self) -> BackendStats:
        return self._stats

    def close(self) -> None:
        pass


class _FailingBackend:
    def count_stats(self) -> BackendStats:
        raise RuntimeError("cluster unreachable")

    def close(self) -> None:
        pass


def _init_home(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0


def test_backend_kind_local_by_default(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_home(runner, tmp_path, monkeypatch)
    with AppContext.bootstrap() as ctx:
        assert _backend_kind(ctx) == "local"
        data = collect_status(ctx)
        assert data["backend"] == {"type": "local"}


def test_backend_kind_server_opensearch(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_home(runner, tmp_path, monkeypatch)
    with AppContext.bootstrap() as ctx:
        ctx.config.storage.mode = "server"
        ctx.config.storage.server.engine = "opensearch"
        assert _backend_kind(ctx) == "server/opensearch"


def test_backend_kind_server_elasticsearch(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_home(runner, tmp_path, monkeypatch)
    with AppContext.bootstrap() as ctx:
        ctx.config.storage.mode = "server"
        ctx.config.storage.server.engine = "elasticsearch"
        assert _backend_kind(ctx) == "server/elasticsearch"


def test_collect_status_reports_real_server_counts(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_home(runner, tmp_path, monkeypatch)
    stats = BackendStats(files=3, entities=5, document_units=2, embeddings=7)
    with AppContext.bootstrap() as ctx:
        ctx.config.storage.mode = "server"
        ctx.config.storage.server.engine = "opensearch"
        ctx._server_backend = _StatsBackend(stats)  # type: ignore[assignment]
        data = collect_status(ctx)

    assert data["backend"]["type"] == "server/opensearch"
    assert data["backend"]["counts"] == {
        "files": 3,
        "entities": 5,
        "document_units": 2,
        "embeddings": 7,
    }
    assert "error" not in data["backend"]


def test_collect_status_reports_error_when_server_unreachable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_home(runner, tmp_path, monkeypatch)
    with AppContext.bootstrap() as ctx:
        ctx.config.storage.mode = "server"
        ctx.config.storage.server.engine = "opensearch"
        ctx._server_backend = _FailingBackend()  # type: ignore[assignment]
        data = collect_status(ctx)

    assert "counts" not in data["backend"]
    assert "error" in data["backend"]
    assert "unreachable" in data["backend"]["error"]


def test_local_mode_status_json_output_unchanged_shape(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode's ``sources``/``totals`` blocks are unaffected by the
    new ``backend`` key -- this is purely additive.
    """
    _init_home(runner, tmp_path, monkeypatch)
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    import json

    payload = json.loads(result.output)["data"]
    assert payload["backend"] == {"type": "local"}
    assert "sources" in payload
    assert "totals" in payload
