"""End-to-end coverage for the indexing benchmark harness (indexing
optimization plan, Phase P0): ``run_scenario`` must actually drive the
real ``run_source_pass`` entry point and produce sane, internally
consistent metrics -- not just avoid throwing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.indexing import fixtures
from benchmarks.indexing.runner import run_scenario


@pytest.fixture(autouse=True)
def _disable_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keeps this test fast and independent of whether Docling/torch are
    # installed in the environment running it -- see
    # ``benchmarks/indexing/__main__.py``'s own docling_available check
    # for how the real benchmark CLI handles this.
    monkeypatch.setenv("RAGMONK_DOCUMENTS__ENABLED", "false")
    monkeypatch.setenv("RAGMONK_UPDATES__ENABLED", "false")


def test_cold_then_warm_scenario_counts_are_consistent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source_root = tmp_path / "source"
    source_root.mkdir()
    files = fixtures.generate_code_corpus(source_root, 10)

    cold_metrics, cold_correctness = run_scenario(
        home=home, source_path=source_root, scenario="cold_index", corpus_tier="test"
    )
    assert cold_metrics.scanned == 10
    assert cold_metrics.new == 10
    assert cold_metrics.changed == 0
    assert cold_metrics.unchanged == 0
    assert cold_metrics.failed == 0
    assert cold_metrics.wall_time_s >= 0.0
    assert cold_correctness.entities >= 0

    warm_metrics, _ = run_scenario(
        home=home, source_path=source_root, scenario="warm_unchanged", corpus_tier="test"
    )
    assert warm_metrics.scanned == 10
    assert warm_metrics.new == 0
    assert warm_metrics.changed == 0
    assert warm_metrics.unchanged == 10
    # A warm no-op pass must not re-hash every file (the whole point of
    # size/mtime fast-skip, already implemented -- see
    # already_implemented_do_not_duplicate in the plan).
    assert warm_metrics.hash_calls == 0

    fixtures.apply_single_edit(files, seed=0)
    edit_metrics, _ = run_scenario(
        home=home, source_path=source_root, scenario="single_edit", corpus_tier="test"
    )
    assert edit_metrics.scanned == 10
    assert edit_metrics.changed == 1
    assert edit_metrics.unchanged == 9
    assert edit_metrics.hash_calls == 1


def test_delete_scenario_reduces_scanned_and_reports_deletion(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source_root = tmp_path / "source"
    source_root.mkdir()
    files = fixtures.generate_code_corpus(source_root, 6)

    run_scenario(home=home, source_path=source_root, scenario="cold_index", corpus_tier="test")
    fixtures.apply_delete(files, 2, seed=0)
    metrics, _ = run_scenario(
        home=home, source_path=source_root, scenario="delete", corpus_tier="test"
    )
    assert metrics.scanned == 4
    assert metrics.deleted == 2


def test_mixed_document_corpus_indexes_without_documents_enabled(tmp_path: Path) -> None:
    """With ``RAGMONK_DOCUMENTS__ENABLED=false`` (set by the autouse
    fixture above), document-kind files still route through the default
    raw processor and get marked INDEXED rather than erroring -- the
    benchmark harness must remain usable in an environment without
    Docling/torch installed.
    """
    home = tmp_path / "home"
    source_root = tmp_path / "source"
    source_root.mkdir()
    fixtures.generate_mixed_document_corpus(source_root, 4)

    metrics, _ = run_scenario(
        home=home, source_path=source_root, scenario="cold_index", corpus_tier="test"
    )
    assert metrics.scanned == 4
    assert metrics.new == 4
    assert metrics.failed == 0
