"""End-to-end document indexing via the real CLI: a mixed-format
directory indexes cleanly, a corrupt document-kind file is isolated
(Phase 1/2's "poisoned file" pattern applied to Phase 3's
``document_processor``), and an oversized PDF is marked
``SKIPPED_LIMIT`` rather than crashing or being silently dropped.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import ragmonk.indexing.coordinator as coordinator_module
from ragmonk.cli.main import app
from ragmonk.core import paths
from ragmonk.core.errors import EXIT_INDEXING_PARTIAL_FAILURE
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import documents_repo
from ragmonk.storage.sqlite import connect

FIXTURES = Path(__file__).parent.parent / "fixtures" / "documents"
SOURCE_ID_RE = re.compile(r"Added source (\S+)")


def _add_source(runner: CliRunner, path: Path) -> str:
    result = runner.invoke(app, ["source", "add", str(path)])
    assert result.exit_code == 0, result.output
    match = SOURCE_ID_RE.search(result.output)
    assert match is not None, result.output
    return match.group(1)


def _knowledge_conn(home: Path, source_path: Path):  # noqa: ANN201 - test helper
    project_id = paths.project_id_for_path(source_path)
    conn = connect(paths.project_db_path(project_id, home))
    apply_migrations(conn, "knowledge")
    return conn


@pytest.fixture(autouse=True)
def _force_permanent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same rationale as test_code_indexing.py: real backoff needs 5 index
    # runs before a failure is permanent; force it on the first attempt.
    monkeypatch.setattr(coordinator_module.retry, "is_permanent", lambda attempt: True)


def test_mixed_format_directory_isolates_corrupt_document(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "docs_project"
    root.mkdir()
    for name in ("simple.md", "simple.txt", "simple.html", "sample.eml", "corrupt.docx"):
        shutil.copy(FIXTURES / name, root / name)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)

    result = runner.invoke(app, ["index"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert result.exit_code == EXIT_INDEXING_PARTIAL_FAILURE

    status = runner.invoke(app, ["status", "--json"])
    assert status.exit_code == 0
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    assert totals.get("failed", 0) == 1
    assert totals.get("indexed", 0) == 4


def test_docs_cli_and_fts_reflect_indexed_documents(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "docs_project"
    root.mkdir()
    for name in ("simple.md", "document.docx", "spreadsheet.xlsx", "presentation.pptx"):
        shutil.copy(FIXTURES / name, root / name)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    source_id = _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output

    docs_result = runner.invoke(app, ["docs", "--json"])
    assert docs_result.exit_code == 0, docs_result.output
    payload = json.loads(docs_result.output)
    assert payload["schema_version"] == "1"
    rows = payload["data"]["documents"]
    assert len(rows) == 4
    by_format = {row["format"] for row in rows}
    assert by_format == {"markdown", "docx", "xlsx", "pptx"}
    assert all(row["status"] == "indexed" for row in rows)

    markdown_row = next(row for row in rows if row["format"] == "markdown")
    assert markdown_row["title"] == "Title Heading"
    assert markdown_row["section_count"] == 2
    assert markdown_row["table_count"] == 1

    filtered = runner.invoke(app, ["docs", "--source", source_id, "--json"])
    assert filtered.exit_code == 0
    assert len(json.loads(filtered.output)["data"]["documents"]) == 4

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        hits = documents_repo.search_fts(conn, "Section")
        assert any(h["document_id"] is not None for h in hits)
        docx_hits = documents_repo.search_fts(conn, "alpha")
        assert len(docx_hits) >= 1
    finally:
        conn.close()


def test_phase_10_formats_index_and_are_fts_searchable(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search Quality Improvement Plan, Phase 10: CSV/ODT/ODS/ODP/EPUB
    convert, normalize, chunk, and store correctly end-to-end, and their
    real extracted content is findable through the same FTS path every
    earlier format already uses.
    """
    root = tmp_path / "docs_project"
    root.mkdir()
    names = ("simple.csv", "document.odt", "spreadsheet.ods", "presentation.odp", "sample.epub")
    for name in names:
        shutil.copy(FIXTURES / name, root / name)
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output

    docs_result = runner.invoke(app, ["docs", "--json"])
    assert docs_result.exit_code == 0, docs_result.output
    rows = json.loads(docs_result.output)["data"]["documents"]
    assert len(rows) == 5
    by_format = {row["format"] for row in rows}
    assert by_format == {"csv", "odt", "ods", "odp", "epub"}
    assert all(row["status"] == "indexed" for row in rows)

    odt_row = next(row for row in rows if row["format"] == "odt")
    assert odt_row["title"] == "Doc Title"
    assert odt_row["section_count"] == 2
    assert odt_row["paragraph_count"] == 2

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        # CSV/ODS both store the same table -- content the FTS index has
        # to find regardless of which spreadsheet-like format it came
        # from.
        alpha_hits = documents_repo.search_fts(conn, "alpha")
        assert len(alpha_hits) >= 2
        # ODT's paragraph text.
        assert documents_repo.search_fts(conn, "Paragraph in section one")
        # EPUB's chapter heading and body.
        assert documents_repo.search_fts(conn, "Chapter One")
        assert documents_repo.search_fts(conn, "sample EPUB book")
        # ODP's slide title.
        assert documents_repo.search_fts(conn, "Presentation Title")
    finally:
        conn.close()


def test_image_ocr_disabled_by_default_indexes_without_derived_content(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``documents.image_ocr`` defaults to ``False`` -- an image-kind file
    is still indexed (as a file record) but never actually OCR'd, exactly
    like an unsupported document extension. See
    ``test_docling_pdf.py::test_image_ocr_enabled_extracts_real_text_via_ocr``
    for the opt-in, real-OCR counterpart.
    """
    root = tmp_path / "docs_project"
    root.mkdir()
    shutil.copy(FIXTURES / "sample_ocr.png", root / "sample_ocr.png")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output

    docs_result = runner.invoke(app, ["docs", "--json"])
    rows = json.loads(docs_result.output)["data"]["documents"]
    assert len(rows) == 1
    assert rows[0]["status"] == "indexed"
    assert rows[0]["format"] is None

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        assert documents_repo.count_all(conn) == 0
    finally:
        conn.close()


@pytest.mark.docling_pdf
def test_image_ocr_enabled_indexes_real_ocr_text_end_to_end(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in counterpart to
    ``test_image_ocr_disabled_by_default_indexes_without_derived_content``:
    with ``documents.image_ocr`` on, the real OCR engine actually runs
    (network + model download on first use, same shape as
    ``docling_pdf``'s own PDF model download -- see that marker's
    ``test_docling_pdf.py`` docstring), and the recovered text is stored
    and FTS-searchable through the exact same path every other format
    uses.
    """
    monkeypatch.setenv("RAGMONK_DOCUMENTS__IMAGE_OCR", "true")
    root = tmp_path / "docs_project"
    root.mkdir()
    shutil.copy(FIXTURES / "sample_ocr.png", root / "sample_ocr.png")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    index_result = runner.invoke(app, ["index"])
    assert index_result.exit_code == 0, index_result.output

    docs_result = runner.invoke(app, ["docs", "--json"])
    rows = json.loads(docs_result.output)["data"]["documents"]
    assert len(rows) == 1
    assert rows[0]["status"] == "indexed"
    assert rows[0]["format"] == "image"

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        assert documents_repo.search_fts(conn, "Sample Image Title")
        assert documents_repo.search_fts(conn, "OCR body text")
    finally:
        conn.close()


def test_indexed_paragraph_stores_contextualized_embedding_text(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search Quality Improvement Plan, Phase 3, end-to-end through the
    real pipeline (Docling parsing -> normalize -> chunk -> store): a
    paragraph's persisted ``embedding_text`` carries the document title
    and heading path the isolated, raw paragraph text alone does not --
    this is what ``indexing/embedding_indexer.py`` actually embeds (see
    ``tests/unit/test_embedding_indexer.py`` for that seam directly).
    """
    root = tmp_path / "docs_project"
    root.mkdir()
    shutil.copy(FIXTURES / "simple.md", root / "simple.md")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    assert runner.invoke(app, ["index"]).exit_code == 0

    conn = _knowledge_conn(ragmonk_home, root)
    try:
        rows = conn.execute(
            "SELECT text, embedding_text FROM document_sections "
            "WHERE kind = 'paragraph' AND text LIKE 'Paragraph in section one%'"
        ).fetchall()
        assert len(rows) == 1
        raw_text = rows[0]["text"]
        embedding_text = rows[0]["embedding_text"]
        assert raw_text == "Paragraph in section one."
        # The raw text (shown to users as evidence) is untouched.
        assert embedding_text != raw_text
        assert embedding_text.startswith("Document: Title Heading\nSection: ")
        assert "Section One" in embedding_text
        assert embedding_text.endswith(raw_text)
    finally:
        conn.close()


def test_oversized_pdf_is_skipped_limit_without_model_download(
    ragmonk_home: Path, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # documents.max_pages=0 rejects the fixture's 1-page PDF via
    # docling_adapter.pdf_page_count (pypdfium2 only) *before*
    # docling_adapter.convert() would ever run -- so this test never
    # touches Docling's ML pipeline and needs no ``docling_pdf`` marker.
    monkeypatch.setenv("RAGMONK_DOCUMENTS__MAX_PAGES", "0")
    root = tmp_path / "docs_project"
    root.mkdir()
    shutil.copy(FIXTURES / "sample.pdf", root / "sample.pdf")
    monkeypatch.chdir(tmp_path)

    assert runner.invoke(app, ["init"]).exit_code == 0
    _add_source(runner, root)
    result = runner.invoke(app, ["index"])
    assert result.exit_code == 0, result.output

    status = runner.invoke(app, ["status", "--json"])
    totals = json.loads(status.output)["data"]["totals"]["by_status"]
    assert totals.get("skipped_limit", 0) == 1
    assert totals.get("failed", 0) == 0

    docs_result = runner.invoke(app, ["docs", "--json"])
    rows = json.loads(docs_result.output)["data"]["documents"]
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped_limit"
    assert rows[0]["format"] is None
