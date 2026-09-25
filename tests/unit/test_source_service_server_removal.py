"""Storage backend abstraction plan, Phase 8: source removal in server
mode must purge the source's searchable knowledge from the real backend
(``ctx.backend().clear_source(source_id)``) -- not just the local
control-plane bookkeeping (sources registry row / project dir), which
stays local regardless of storage mode. Covers both call sites:
``ragmonk source remove`` (``cli/source.py``) and the admin-UI-shared
``service.source_service.remove_source``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ragmonk.cli.main import app
from ragmonk.core.lifecycle import AppContext
from ragmonk.service import source_service


class _RecordingBackend:
    def __init__(self) -> None:
        self.cleared_source_ids: list[str] = []

    def clear_source(self, source_id: str) -> None:
        self.cleared_source_ids.append(source_id)

    def count_stats(self) -> Any:
        from ragmonk.backends.models import BackendStats

        return BackendStats()

    def health(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _add_local_source(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Registers a source through the ordinary local-mode CLI path (the
    sources registry/control-plane bookkeeping is always local, even in
    server mode -- see ``AppContext.backend``'s docstring), so both tests
    below exercise a real, registered source rather than a hand-built
    fixture row.
    """
    import re

    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("def foo():\n    return 1\n")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["source", "add", str(source_dir)])
    assert result.exit_code == 0, result.output
    match = re.search(r"Added source (\S+)", result.output)
    assert match is not None
    return match.group(1)


def test_cli_source_remove_calls_backend_clear_source_in_server_mode(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _add_local_source(runner, tmp_path, monkeypatch)

    recording = _RecordingBackend()
    original_bootstrap = AppContext.bootstrap

    def _patched_bootstrap(*args: Any, **kwargs: Any) -> AppContext:
        ctx = original_bootstrap(*args, **kwargs)
        ctx.config.storage.mode = "server"
        ctx._server_backend = recording  # type: ignore[assignment]
        return ctx

    monkeypatch.setattr(AppContext, "bootstrap", _patched_bootstrap)

    result = runner.invoke(app, ["source", "remove", source_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert recording.cleared_source_ids == [source_id]


def test_source_service_remove_source_calls_backend_clear_source_in_server_mode(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = _add_local_source(runner, tmp_path, monkeypatch)

    recording = _RecordingBackend()
    with AppContext.bootstrap() as ctx:
        ctx.config.storage.mode = "server"
        ctx._server_backend = recording  # type: ignore[assignment]
        source_service.remove_source(ctx, source_id)

    assert recording.cleared_source_ids == [source_id]


def test_source_service_remove_source_skips_backend_call_in_local_mode(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode's existing behavior is unchanged -- no backend call at
    all (there is nothing to purge remotely).
    """
    source_id = _add_local_source(runner, tmp_path, monkeypatch)

    with AppContext.bootstrap() as ctx:
        assert ctx.config.storage.mode == "local"
        outcome = source_service.remove_source(ctx, source_id)

    assert outcome.source.id == source_id
