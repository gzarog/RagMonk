from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.cli.main import app
from ragmonk.core.errors import (
    EXIT_CONFIG_ERROR,
    EXIT_INDEXING_PARTIAL_FAILURE,
    EXIT_INVALID_ARGUMENTS,
    EXIT_SOURCE_UNAVAILABLE,
)


def test_missing_required_argument_is_invalid_arguments(
    ragmonk_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["source", "add"])
    assert result.exit_code == EXIT_INVALID_ARGUMENTS


def test_malformed_config_is_config_error(
    ragmonk_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    ragmonk_home.mkdir(parents=True, exist_ok=True)
    (ragmonk_home / "config.yaml").write_text("runtime:\n  max_workers: \"not-an-int\"\n")
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == EXIT_CONFIG_ERROR


def test_add_nonexistent_source_is_source_unavailable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(app, ["source", "add", str(tmp_path / "does-not-exist")])
    assert result.exit_code == EXIT_SOURCE_UNAVAILABLE


def test_partial_indexing_failure_is_reported_and_does_not_crash(
    ragmonk_home: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``.dat`` is neither a code nor a document extension (sources.detector
    # .classify -> FileKind.UNKNOWN), so cli/index.py never overrides it
    # with the real CodeProcessor (Phase 2) or DocumentProcessor (Phase 3)
    # -- this generic "a poisoned processor must not crash the CLI" test
    # keeps exercising the default registry's raw_processor path it
    # patches below. Phase 2/3's own real-processor crash-isolation is
    # covered by test_code_indexing.py and test_document_indexing.py.
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "good1.dat").write_text("ok")
    (source_dir / "good2.dat").write_text("ok too")
    (source_dir / "poison.dat").write_text("boom")
    monkeypatch.chdir(tmp_path)

    original_raw_processor = coordinator_module.raw_processor

    def poisoned_processor(
        ctx: coordinator_module.ProcessorContext,
    ) -> coordinator_module.ProcessingOutcome:
        if ctx.path.name == "poison.dat":
            raise ValueError("simulated permanent parser failure")
        return original_raw_processor(ctx)

    # Force every attempt to look permanent so the test doesn't depend on
    # the real retry attempt threshold.
    monkeypatch.setattr(coordinator_module.retry, "is_permanent", lambda attempt: True)
    monkeypatch.setattr(coordinator_module, "raw_processor", poisoned_processor)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0

    result = runner.invoke(app, ["index"])
    # A controlled exit (SystemExit carrying our code) proves the process
    # did not crash on an unhandled traceback despite the poisoned file.
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE

    status = runner.invoke(app, ["status", "--json"])
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    assert totals.get("indexed", 0) == 2
    assert totals.get("failed", 0) == 1


def test_doctor_reports_warning_when_source_unreachable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Phase 7's offline-vs-deleted safety fix: an unreachable source root
    # (unmounted network share, deleted directory) must never fail
    # `doctor` outright -- see indexing/coordinator.py and
    # cli/doctor.py's "Sources" check.
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "a.py").write_text("print('a')")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["source", "add", str(source_dir)]).exit_code == 0
    shutil.rmtree(source_dir)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0

    payload = json.loads(result.output)
    assert payload["data"]["result"] == "HEALTHY WITH WARNINGS"
    sources_section = next(
        s for s in payload["data"]["sections"] if s["name"] == "Sources"
    )
    assert sources_section["checks"][0]["status"] == "warn"
