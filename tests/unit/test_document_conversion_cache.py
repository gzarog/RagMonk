"""Storage-level tests for ``document_conversion_cache``: get/put
round-trip, cache_version mismatch treated as a miss, and upsert
overwrite -- pure SQLite, no Docling/PDF/ML model involved (see
``test_docling_pdf.py`` for the real end-to-end cache proof).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragpilot.storage.migrations import apply_migrations
from ragpilot.storage.repositories import document_conversion_cache_repo
from ragpilot.storage.repositories.document_conversion_cache_repo import CachedConversion
from ragpilot.storage.sqlite import connect, transaction


@pytest.fixture
def conn(tmp_path: Path):  # noqa: ANN201
    connection = connect(tmp_path / "knowledge.db")
    apply_migrations(connection, "knowledge")
    yield connection
    connection.close()


def test_get_on_empty_cache_is_a_miss(conn) -> None:  # noqa: ANN001
    assert document_conversion_cache_repo.get(conn, "deadbeef", cache_version=1) is None


def test_put_then_get_round_trips(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        document_conversion_cache_repo.put(
            conn,
            CachedConversion(
                content_hash="hash-a",
                serialized_document='{"schema_name": "DoclingDocument"}',
                serialization_format="docling_document/json",
                page_count=3,
                parser_version="2.127.0",
            ),
            cache_version=1,
            created_at="2026-01-01T00:00:00+00:00",
        )

    cached = document_conversion_cache_repo.get(conn, "hash-a", cache_version=1)
    assert cached is not None
    assert cached.content_hash == "hash-a"
    assert cached.serialized_document == '{"schema_name": "DoclingDocument"}'
    assert cached.serialization_format == "docling_document/json"
    assert cached.page_count == 3
    assert cached.parser_version == "2.127.0"


def test_get_with_mismatched_cache_version_is_a_miss(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        document_conversion_cache_repo.put(
            conn,
            CachedConversion(
                content_hash="hash-a",
                serialized_document="{}",
                serialization_format="docling_document/json",
                page_count=1,
                parser_version="2.127.0",
            ),
            cache_version=1,
            created_at="2026-01-01T00:00:00+00:00",
        )

    # A row written under an older cache_version must never be handed back
    # as if it matched the caller's current serialization scheme -- this is
    # also what makes a pre-Phase-1B row (written when this table cached
    # Markdown text under the ``markdown`` column ``serialized_document``
    # was renamed from) a guaranteed miss once ``docling_adapter``'s
    # ``_CACHE_VERSION`` is bumped, without any data migration.
    assert document_conversion_cache_repo.get(conn, "hash-a", cache_version=2) is None


def test_put_upserts_overwriting_the_existing_row(conn) -> None:  # noqa: ANN001
    with transaction(conn):
        document_conversion_cache_repo.put(
            conn,
            CachedConversion(
                content_hash="hash-a",
                serialized_document="old",
                serialization_format="docling_document/json",
                page_count=1,
                parser_version="2.127.0",
            ),
            cache_version=1,
            created_at="2026-01-01T00:00:00+00:00",
        )
    with transaction(conn):
        document_conversion_cache_repo.put(
            conn,
            CachedConversion(
                content_hash="hash-a",
                serialized_document="new",
                serialization_format="docling_document/json",
                page_count=2,
                parser_version="2.128.0",
            ),
            cache_version=1,
            created_at="2026-01-02T00:00:00+00:00",
        )

    cached = document_conversion_cache_repo.get(conn, "hash-a", cache_version=1)
    assert cached is not None
    assert cached.serialized_document == "new"
    assert cached.page_count == 2
    assert cached.parser_version == "2.128.0"

    row_count = conn.execute("SELECT COUNT(*) AS n FROM document_conversion_cache").fetchone()["n"]
    assert row_count == 1


def test_docling_document_json_round_trips_headings_paragraphs_and_tables() -> None:
    """Proves the serialization scheme ``docling_adapter._convert_pdf``
    relies on (``DoclingDocument.model_dump_json()`` /
    ``model_validate_json()``) round-trips every item shape RAGpilot's
    normalizer reads -- title, heading, paragraph, table, and each item's
    real page ``prov`` -- losslessly. Pure pydantic, no Docling PDF
    pipeline or ML model involved.
    """
    from docling_core.types.doc import DocItemLabel, TableCell, TableData
    from docling_core.types.doc.base import BoundingBox
    from docling_core.types.doc.common.reference import ProvenanceItem
    from docling_core.types.doc.document import DoclingDocument

    def prov(page_no: int) -> ProvenanceItem:
        return ProvenanceItem(
            page_no=page_no, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1)
        )

    doc = DoclingDocument(name="synthetic")
    doc.add_title("Doc Title", prov=prov(1))
    doc.add_heading("Section One", level=1, prov=prov(1))
    doc.add_text(DocItemLabel.TEXT, "Body paragraph.", prov=prov(1))
    table_data = TableData(
        num_rows=2,
        num_cols=2,
        table_cells=[
            TableCell(
                text="Name",
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=0,
                end_col_offset_idx=1,
            ),
            TableCell(
                text="Value",
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=1,
                end_col_offset_idx=2,
            ),
            TableCell(
                text="a",
                start_row_offset_idx=1,
                end_row_offset_idx=2,
                start_col_offset_idx=0,
                end_col_offset_idx=1,
            ),
            TableCell(
                text="1",
                start_row_offset_idx=1,
                end_row_offset_idx=2,
                start_col_offset_idx=1,
                end_col_offset_idx=2,
            ),
        ],
    )
    doc.add_table(data=table_data, prov=prov(2))

    restored = DoclingDocument.model_validate_json(doc.model_dump_json())

    assert restored == doc
    original_items = list(doc.iterate_items())
    restored_items = list(restored.iterate_items())
    assert len(original_items) == len(restored_items)
    for (orig_item, _), (new_item, _) in zip(original_items, restored_items, strict=True):
        assert type(orig_item) is type(new_item)
        assert getattr(orig_item, "prov", None) == getattr(new_item, "prov", None)
