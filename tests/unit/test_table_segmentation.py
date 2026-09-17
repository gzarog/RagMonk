"""Exact Tokenizer plan, Phase 3: oversized table row/cell segmentation.

Covers ``table_renderer.segment_oversized_row`` directly plus the
chunker's end-to-end table budgeting, including the diagnostics counters.
Runs in the default (offline) suite.
"""

from __future__ import annotations

from ragmonk.core.config import ChunkingConfig
from ragmonk.documents import table_renderer
from ragmonk.documents.chunker import ChunkingDiagnostics, chunk_document
from ragmonk.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragmonk.documents.tokenization import count_tokens
from ragmonk.tokenization.model_tokenizer import get_model_tokenizer


def _table_unit(rows, *, header_row_count=1, caption=None):  # noqa: ANN001, ANN202
    return NormalizedUnit(
        kind="table",
        text="",
        heading_level=None,
        heading_path=("H1",),
        parent_index=0,
        page_start=1,
        page_end=1,
        table_rows=rows,
        header_row_count=header_row_count,
        caption=caption,
    )


def _doc(units) -> NormalizedDocument:  # noqa: ANN001
    return NormalizedDocument(title=None, page_count=None, is_scanned=False, units=units)


def _segment_tokens(header_slice, data_slice, overhead: str) -> int:  # noqa: ANN001
    rendered_header = table_renderer.render_rows(header_slice)
    rendered_data = table_renderer.render_rows([data_slice])
    parts = [p for p in (overhead, rendered_header, rendered_data) if p]
    return count_tokens("\n\n".join(parts))


def test_segment_oversized_row_splits_at_column_boundaries() -> None:
    header = ("Alpha", "Beta", "Gamma", "Delta")
    # Each cell fits on its own but the whole row does not, so the row is
    # split at column boundaries with no token-level cell splitting -- so
    # each column appears exactly once and coverage is preserved in order.
    cell = "the cat dog run sun map car box pen cup"  # 10 single-token words
    row = (cell, cell, cell, cell)
    segments = table_renderer.segment_oversized_row(
        row, header_rows=(header,), max_tokens=40, fixed_overhead="Row 1"
    )
    assert len(segments) > 1
    recovered_cols: list[str] = []
    for header_slice, data_slice in segments:
        assert _segment_tokens(header_slice, data_slice, "Row 1") <= 40
        recovered_cols.extend(header_slice[0])
    assert recovered_cols == list(header)


def test_segment_oversized_single_cell_is_token_split_keeping_column_name() -> None:
    header = ("OnlyColumn",)
    big = " ".join(f"tok{i:03d}" for i in range(200))
    row = (big,)
    segments = table_renderer.segment_oversized_row(
        header_rows=(header,), row=row, max_tokens=24, fixed_overhead="Row 7"
    )
    assert len(segments) > 1
    for header_slice, data_slice in segments:
        assert header_slice == (("OnlyColumn",),)  # column name repeated per fragment
        assert _segment_tokens(header_slice, data_slice, "Row 7") <= 24


def test_chunker_counts_oversized_rows_and_cells() -> None:
    big_cell = " ".join(f"cell{i:04d}" for i in range(120))
    unit = _table_unit((("Metric", "Value"), ("throughput", big_cell)))
    cfg = ChunkingConfig(max_tokens=48, min_tokens=1, overlap_tokens=0, safety_tokens=0)
    diagnostics = ChunkingDiagnostics()

    chunks = chunk_document(_doc([unit]), config=cfg, diagnostics=diagnostics)
    tables = [c for c in chunks if c.kind == "table"]
    tok = get_model_tokenizer()
    bound = cfg.resolved_max_tokens - cfg.safety_tokens

    assert len(tables) > 1
    for chunk in tables:
        assert tok.count(chunk.contextual_text, add_special_tokens=True) <= bound
    assert diagnostics.oversized_table_rows == 1
    assert diagnostics.oversized_table_cells == 1  # the "Value" cell overflowed alone
    assert diagnostics.max_payload_tokens <= bound


def test_normal_table_is_not_segmented() -> None:
    unit = _table_unit((("A", "B"), ("1", "2"), ("3", "4")))
    diagnostics = ChunkingDiagnostics()
    chunks = chunk_document(_doc([unit]), diagnostics=diagnostics)
    assert len([c for c in chunks if c.kind == "table"]) == 1
    assert diagnostics.oversized_table_rows == 0
    assert diagnostics.oversized_table_cells == 0
