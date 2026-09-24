"""Unit coverage for the indexing benchmark harness itself (indexing
optimization plan, Phase P0) -- the fixture generator and hash-call
counter, not the real indexing pipeline they drive (that is exercised
end-to-end by ``benchmarks.indexing.runner.run_scenario`` in
``tests/integration/test_benchmarks_indexing_integration.py``).
"""

from __future__ import annotations

from pathlib import Path

from benchmarks.indexing import fixtures
from benchmarks.indexing.metrics import count_hash_calls, environment_info, measure_resources

from ragmonk.sources import fingerprint as fingerprint_module


def test_generate_code_corpus_is_deterministic_and_parseable(tmp_path: Path) -> None:
    written = fixtures.generate_code_corpus(tmp_path, 25)
    assert len(written) == 25
    assert all(p.exists() for p in written)
    assert all(p.suffix == ".py" for p in written)
    # Deterministic: same index always produces byte-identical content.
    assert written[0].read_text(encoding="utf-8") == written[0].read_text(encoding="utf-8")
    compiled_ok = 0
    for path in written[:5]:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        compiled_ok += 1
    assert compiled_ok == 5


def test_generate_mixed_document_corpus_reuses_real_fixture_content(tmp_path: Path) -> None:
    written = fixtures.generate_mixed_document_corpus(tmp_path, 8)
    assert len(written) == 8
    suffixes = {p.suffix for p in written}
    assert suffixes == {".txt", ".md", ".html", ".csv"}
    assert all(p.stat().st_size > 0 for p in written)


def test_apply_single_edit_changes_exactly_one_file(tmp_path: Path) -> None:
    written = fixtures.generate_code_corpus(tmp_path, 10)
    before = {p: p.read_text(encoding="utf-8") for p in written}
    target = fixtures.apply_single_edit(written, seed=0)
    changed = [p for p in written if p.read_text(encoding="utf-8") != before[p]]
    assert changed == [target]


def test_apply_percent_change_touches_expected_fraction(tmp_path: Path) -> None:
    written = fixtures.generate_code_corpus(tmp_path, 100)
    before = {p: p.read_text(encoding="utf-8") for p in written}
    targets = fixtures.apply_percent_change(written, 0.1, seed=0)
    assert len(targets) == 10
    changed = [p for p in written if p.read_text(encoding="utf-8") != before[p]]
    assert sorted(changed) == sorted(targets)


def test_apply_rename_preserves_content(tmp_path: Path) -> None:
    written = fixtures.generate_code_corpus(tmp_path, 5)
    old_path, new_path = fixtures.apply_rename(written, seed=0)
    assert not old_path.exists()
    assert new_path.exists()
    assert new_path.name == f"renamed_{old_path.name}"


def test_apply_delete_removes_requested_count(tmp_path: Path) -> None:
    written = fixtures.generate_code_corpus(tmp_path, 20)
    deleted = fixtures.apply_delete(written, 5, seed=0)
    assert len(deleted) == 5
    assert all(not p.exists() for p in deleted)
    remaining = [p for p in written if p.exists()]
    assert len(remaining) == 15


def test_count_hash_calls_counts_every_call_and_restores_original(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("hello world", encoding="utf-8")

    original = fingerprint_module.hash_file
    with count_hash_calls() as counters:
        fingerprint_module.hash_file(target, "sha256")
        fingerprint_module.hash_file(target, "sha256")
    assert counters.calls == 2
    assert counters.bytes_hashed == 2 * len("hello world")
    assert counters.total_seconds >= 0.0

    # After the context exits, hash_file is back to its real self and no
    # longer being counted (patch has fully unwound).
    assert fingerprint_module.hash_file is original


def test_measure_resources_reports_positive_wall_time() -> None:
    with measure_resources() as usage:
        sum(range(100_000))
    assert usage["wall_time_s"] >= 0.0
    assert usage["peak_rss_mb"] > 0.0


def test_environment_info_reports_docling_availability() -> None:
    info = environment_info()
    assert "docling_available" in info
    assert isinstance(info["docling_available"], bool)
    assert "python_version" in info
