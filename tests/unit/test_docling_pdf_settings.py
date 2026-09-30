"""Indexing speed: PDF conversion settings (fast mode, table structure,
worker processes) in ``documents/docling_adapter.py``.

Everything here runs in the default suite without Docling's ML models:
fast mode never loads them, and the worker-process plumbing is exercised
with an inline stand-in executor. The one real multi-process conversion
is gated behind the ``docling_pdf`` marker like the other real-model tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any

import pytest

from ragmonk.core.config import DocumentsConfig
from ragmonk.documents import docling_adapter as adapter
from ragmonk.documents import normalizer
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"
SAMPLE_PDF = FIXTURES / "sample.pdf"


@pytest.fixture(autouse=True)
def _restore_settings() -> Iterator[None]:
    yield
    adapter.configure_pdf(adapter.PdfSettings())


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def test_defaults_keep_existing_behavior_and_versions() -> None:
    cfg = DocumentsConfig()
    assert (cfg.pdf_mode, cfg.pdf_table_structure, cfg.pdf_process_workers) == (
        "accurate",
        True,
        1,
    )
    assert adapter.parser_version() == adapter.PARSER_VERSION
    assert adapter._cache_key("off") == "off"
    assert adapter._cache_key("on") == "on"


def test_invalid_settings_rejected() -> None:
    with pytest.raises(ValueError):
        DocumentsConfig(pdf_mode="turbo")
    with pytest.raises(ValueError):
        DocumentsConfig(pdf_process_workers=0)


def test_non_default_output_settings_change_parser_version_and_cache_key() -> None:
    adapter.configure_pdf(adapter.PdfSettings(mode="fast"))
    assert adapter.parser_version() == f"{adapter.PARSER_VERSION}+pdf=fast"
    assert adapter._cache_key("off") == "fast"
    adapter.configure_pdf(adapter.PdfSettings(table_structure=False))
    assert adapter.parser_version() == f"{adapter.PARSER_VERSION}+tables=off"
    assert adapter._cache_key("off") == "off:notables"
    assert adapter._cache_key("on") == "on:notables"
    # Worker count never changes output, so never forces a reindex.
    adapter.configure_pdf(adapter.PdfSettings(process_workers=4))
    assert adapter.parser_version() == adapter.PARSER_VERSION


def test_fast_mode_extracts_text_with_page_provenance_without_models(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_models() -> Any:
        raise AssertionError("fast mode must not build a Docling converter")

    monkeypatch.setattr(adapter, "_get_converter", no_models)
    adapter.configure_pdf(adapter.PdfSettings(mode="fast"))
    result = adapter.convert(SAMPLE_PDF, conn=conn, ocr_mode="off")
    document = result.document
    assert document.num_pages() == 1
    text = " ".join(item.text for item in document.texts)
    assert "Sample PDF Title" in text
    assert "body text" in text
    assert all(item.prov and item.prov[0].page_no == 1 for item in document.texts)
    normalized = normalizer.normalize(document, adapter.DocumentFormat.PDF)
    assert normalized.units

    # Cached under its own key: an accurate-mode lookup must miss.
    assert adapter._cached_document(conn, adapter.hash_file(SAMPLE_PDF), ocr_used="off") is not None
    adapter.configure_pdf(adapter.PdfSettings())
    assert adapter._cached_document(conn, adapter.hash_file(SAMPLE_PDF), ocr_used="off") is None


def test_fast_mode_unreadable_pdf_raises_conversion_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    adapter.configure_pdf(adapter.PdfSettings(mode="fast"))
    with pytest.raises(adapter.DocumentConversionError):
        adapter.convert(broken)


class _InlineExecutor:
    """Runs submitted work synchronously in-process -- exercises the same
    submit/result/JSON round trip as the real process pool."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[tuple[Any, ...]] = []

    def submit(self, fn: Any, *args: Any) -> Future[Any]:
        self.calls.append(args)
        future: Future[Any] = Future()
        if self.fail_with is not None:
            future.set_exception(self.fail_with)
        else:
            future.set_result(fn(*args))
        return future

    def shutdown(self, **_kwargs: Any) -> None:
        pass


def _fake_pipeline(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> None:
    def fake_run(_converter: Any, path: Path, _source: str) -> Any:
        if fail:
            raise adapter.DocumentConversionError(f"{path}: boom")
        return adapter._fast_text_document(path)

    monkeypatch.setattr(adapter, "_get_converter", lambda: object())
    monkeypatch.setattr(adapter, "_run_conversion", fake_run)


def test_worker_pool_round_trips_document(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pipeline(monkeypatch)
    executor = _InlineExecutor()
    adapter.configure_pdf(adapter.PdfSettings(process_workers=2))
    monkeypatch.setattr(adapter, "_get_pool", lambda: executor)
    document = adapter.convert(SAMPLE_PDF, ocr_mode="off").document
    assert executor.calls == [(str(SAMPLE_PDF), False)]
    assert "Sample PDF Title" in " ".join(item.text for item in document.texts)


def test_worker_conversion_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pipeline(monkeypatch, fail=True)
    adapter.configure_pdf(adapter.PdfSettings(process_workers=2))
    monkeypatch.setattr(adapter, "_get_pool", lambda: _InlineExecutor())
    with pytest.raises(adapter.DocumentConversionError, match="boom"):
        adapter.convert(SAMPLE_PDF, ocr_mode="off")


def test_crashed_worker_fails_only_that_file(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter.configure_pdf(adapter.PdfSettings(process_workers=2))
    monkeypatch.setattr(
        adapter, "_get_pool", lambda: _InlineExecutor(fail_with=BrokenProcessPool("killed"))
    )
    with pytest.raises(adapter.DocumentConversionError, match="worker crashed"):
        adapter.convert(SAMPLE_PDF, ocr_mode="off")


@pytest.mark.docling_pdf
def test_real_process_pool_matches_in_process_conversion() -> None:
    in_process = adapter.convert(SAMPLE_PDF, ocr_mode="off").document
    adapter.configure_pdf(adapter.PdfSettings(process_workers=2))
    pooled = adapter.convert(SAMPLE_PDF, ocr_mode="off").document
    assert [t.text for t in pooled.texts] == [t.text for t in in_process.texts]
