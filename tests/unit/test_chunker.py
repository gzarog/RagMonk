"""Search Quality Improvement Plan, Phase 2: token-aware, hierarchy-aware
chunk boundaries.

Most tests here build a synthetic ``NormalizedDocument``/``NormalizedUnit``
list directly (bypassing Docling entirely) -- fast, deterministic, and
lets each test control token counts precisely via short, predictable
words that are each a single WordPiece token of the exact embedding
tokenizer (see ``tests/unit/test_model_tokenizer.py`` and
``documents/tokenization.py``). ``test_document_normalizer.py`` already
covers the Docling-conversion -> normalize -> chunk golden path per
format; this file is about the packing/splitting/merging algorithm
itself.
"""

from __future__ import annotations

from docling_core.types.doc import DocItemLabel
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table_data import TableCell, TableData

from ragmonk.core.config import ChunkingConfig
from ragmonk.core.models import DocumentFormat
from ragmonk.documents import chunker, normalizer
from ragmonk.documents.chunker import chunk_document
from ragmonk.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragmonk.documents.tokenization import count_tokens


def _heading(
    text: str,
    *,
    level: int = 0,
    heading_path: tuple[str, ...] = (),
    parent_index: int | None = None,
) -> NormalizedUnit:
    return NormalizedUnit(
        kind="heading",
        text=text,
        heading_level=level,
        heading_path=heading_path,
        parent_index=parent_index,
        page_start=None,
        page_end=None,
    )


def _paragraph(
    text: str,
    *,
    heading_path: tuple[str, ...] = (),
    parent_index: int | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> NormalizedUnit:
    return NormalizedUnit(
        kind="paragraph",
        text=text,
        heading_level=None,
        heading_path=heading_path,
        parent_index=parent_index,
        page_start=page_start,
        page_end=page_end,
    )


def _doc(units: list[NormalizedUnit]) -> NormalizedDocument:
    return NormalizedDocument(title=None, page_count=None, is_scanned=False, units=units)


# ---------------------------------------------------------------------------
# Heading boundaries
# ---------------------------------------------------------------------------


def test_paragraphs_never_merge_across_a_heading_boundary() -> None:
    units = [
        _heading("H1"),
        _paragraph("tiny one.", heading_path=("H1",), parent_index=0),
        _heading("H2"),
        _paragraph("tiny two.", heading_path=("H2",), parent_index=2),
    ]
    chunks = chunk_document(_doc(units))
    kinds = [c.kind for c in chunks]
    assert kinds == ["heading", "paragraph", "heading", "paragraph"]
    assert "one" in chunks[1].text and "two" not in chunks[1].text
    assert "two" in chunks[3].text and "one" not in chunks[3].text


def test_nested_headings_preserve_heading_path_through_packing() -> None:
    units = [
        _heading("Doc"),
        _heading("Section", level=1, heading_path=("Doc",), parent_index=0),
        _heading("Subsection", level=2, heading_path=("Doc", "Section"), parent_index=1),
        _paragraph(
            "deep body text.",
            heading_path=("Doc", "Section", "Subsection"),
            parent_index=2,
        ),
    ]
    chunks = chunk_document(_doc(units))
    body = next(c for c in chunks if c.kind == "paragraph")
    assert body.heading_path == ("Doc", "Section", "Subsection")
    assert chunks[body.parent_index].text == "Subsection"
    assert chunks[chunks[body.parent_index].parent_index].text == "Section"


# ---------------------------------------------------------------------------
# Multi-paragraph / multi-page sections
# ---------------------------------------------------------------------------


def test_multiple_short_paragraphs_under_one_heading_merge_into_one_chunk() -> None:
    units = [
        _heading("H1"),
        _paragraph("first short paragraph.", heading_path=("H1",), parent_index=0),
        _paragraph("second short paragraph.", heading_path=("H1",), parent_index=0),
        _paragraph("third short paragraph.", heading_path=("H1",), parent_index=0),
    ]
    chunks = chunk_document(_doc(units))
    assert [c.kind for c in chunks] == ["heading", "paragraph"]
    body = chunks[1]
    assert "first" in body.text and "second" in body.text and "third" in body.text
    assert body.token_count == count_tokens(body.text)


def test_multi_page_section_reports_min_max_page_range() -> None:
    units = [
        _heading("H1"),
        _paragraph(
            "page one text.", heading_path=("H1",), parent_index=0, page_start=1, page_end=1
        ),
        _paragraph(
            "page two text.", heading_path=("H1",), parent_index=0, page_start=2, page_end=2
        ),
        _paragraph(
            "page three text.", heading_path=("H1",), parent_index=0, page_start=3, page_end=3
        ),
    ]
    chunks = chunk_document(_doc(units))
    body = next(c for c in chunks if c.kind == "paragraph")
    assert body.page_start == 1
    assert body.page_end == 3


# ---------------------------------------------------------------------------
# Token budget: splitting very long paragraphs
# ---------------------------------------------------------------------------


def test_very_long_paragraph_splits_by_token_budget_not_char_count() -> None:
    sentences = [f"Sentence number {i} has some plain words in it." for i in range(30)]
    long_text = " ".join(sentences)
    config = ChunkingConfig(max_tokens=40, min_tokens=1, overlap_tokens=0, merge_peers=False)
    units = [_heading("H1"), _paragraph(long_text, heading_path=("H1",), parent_index=0)]

    chunks = chunk_document(_doc(units), config=config)
    body_chunks = [c for c in chunks if c.kind == "paragraph"]

    assert len(body_chunks) > 1
    for c in body_chunks:
        assert c.token_count <= config.max_tokens
        assert c.token_count == count_tokens(c.text)
        assert c.heading_path == ("H1",)
    # No sentence content is dropped: every sentence's distinctive number
    # shows up somewhere across the split chunks, in order.
    joined = " ".join(c.text for c in body_chunks)
    for i in range(30):
        assert f"number {i} has" in joined


def test_no_non_table_chunk_exceeds_max_tokens() -> None:
    rng_words = " ".join(f"word{i:04d}" for i in range(200))
    config = ChunkingConfig(max_tokens=25, min_tokens=5, overlap_tokens=5, merge_peers=True)
    units = [
        _heading("H1"),
        _paragraph("short intro.", heading_path=("H1",), parent_index=0),
        _paragraph(rng_words, heading_path=("H1",), parent_index=0),
        _paragraph("a short trailing remark here.", heading_path=("H1",), parent_index=0),
    ]
    chunks = chunk_document(_doc(units), config=config)
    for c in chunks:
        if c.kind != "table":
            assert c.token_count <= config.max_tokens, (c.kind, c.token_count, c.text)


# ---------------------------------------------------------------------------
# merge_peers: very short paragraphs
# ---------------------------------------------------------------------------


def _four_token_units() -> list[NormalizedUnit]:
    # Every word below is a verified *single* WordPiece token of the exact
    # embedding tokenizer (see test_model_tokenizer.py), so each unit is
    # exactly 4 body tokens and every count in these tests is
    # hand-verifiable. WordPiece pre-splits on whitespace, so a
    # space-joined line's token count is the sum of its words'.
    texts = [
        "the cat dog run",
        "owl map car box",  # "owl" is the marker word (was "MARK")
        "pen cup hat bag",
        "red big sky sea",
        "top bed cow pig",
    ]
    return [_paragraph(t, heading_path=("H1",), parent_index=0) for t in texts]


# Exact Tokenizer plan, Phase 2: max_tokens is now the *full payload*
# ceiling (model special tokens + contextual header + body). With
# safety_tokens=0, the "Section: H1" header (4 tokens) and the 2 special
# tokens, a body budget of 16 needs max_tokens = 16 + 2 + 4 = 22.
_BODY16_MAX_TOKENS = 22


def test_merge_peers_rebalances_an_undersized_trailing_chunk() -> None:
    # Greedy packing alone (no merge_peers) produces [A+B+C+D (16 tok),
    # E (4 tok, < min_tokens=8)] -- the second group can never simply be
    # concatenated onto the first (that's exactly why the packer split
    # there: 16 + 4 = 20 > body budget 16). merge_peers instead rebalances
    # the pair's combined 20 tokens into two even 8/12 halves, both
    # clearing min_tokens=8 without either exceeding the 16-token body
    # budget.
    config = ChunkingConfig(
        max_tokens=_BODY16_MAX_TOKENS, min_tokens=8, overlap_tokens=0, safety_tokens=0
    )
    units = [_heading("H1"), *_four_token_units()]

    chunks = chunk_document(_doc(units), config=config)
    body_chunks = [c for c in chunks if c.kind == "paragraph"]

    assert len(body_chunks) == 2
    assert body_chunks[0].token_count == 8
    assert body_chunks[1].token_count == 12
    assert "the" in body_chunks[0].text and "owl" in body_chunks[0].text
    assert "pen" in body_chunks[1].text and "top" in body_chunks[1].text
    for c in body_chunks:
        assert c.token_count >= config.min_tokens


def test_merge_peers_disabled_keeps_the_undersized_chunk_standalone() -> None:
    config = ChunkingConfig(
        max_tokens=_BODY16_MAX_TOKENS,
        min_tokens=8,
        overlap_tokens=0,
        safety_tokens=0,
        merge_peers=False,
    )
    units = [_heading("H1"), *_four_token_units()]

    chunks = chunk_document(_doc(units), config=config)
    body_chunks = [c for c in chunks if c.kind == "paragraph"]

    assert len(body_chunks) == 2
    assert body_chunks[0].token_count == 16
    assert body_chunks[1].token_count == 4  # left standalone, below min_tokens=8


# ---------------------------------------------------------------------------
# overlap_tokens: within a section, never across a heading boundary
# ---------------------------------------------------------------------------


def test_overlap_applies_within_a_section_but_never_crosses_a_heading() -> None:
    # 8 verified single-token words per unit == exactly 8 body tokens each.
    unit_a = _paragraph(
        "the cat dog run sun map car box", heading_path=("H1",), parent_index=0
    )
    unit_b = _paragraph(
        "owl cup hat bag red big sky sea", heading_path=("H1",), parent_index=0
    )
    unit_c = _paragraph(
        "ant bed cow pig fox log mud net", heading_path=("H1",), parent_index=0
    )
    unit_d = _paragraph(
        "jar key top oak the cat dog run", heading_path=("H2",), parent_index=4
    )
    # Body budget 20 under the "Section: H1" header (4 tok) + 2 special
    # tokens, with safety_tokens=0: max_tokens = 20 + 2 + 4 = 26.
    config = ChunkingConfig(
        max_tokens=26, min_tokens=1, overlap_tokens=10, safety_tokens=0
    )
    units = [_heading("H1"), unit_a, unit_b, unit_c, _heading("H2"), unit_d]

    chunks = chunk_document(_doc(units), config=config)
    h1_body = [c for c in chunks if c.kind == "paragraph" and c.heading_path == ("H1",)]
    h2_body = [c for c in chunks if c.kind == "paragraph" and c.heading_path == ("H2",)]

    assert len(h1_body) == 2
    # unit_b ("owl...") is short enough to seed the second H1 chunk's
    # overlap, so it legitimately appears in both.
    assert "owl" in h1_body[0].text
    assert "owl" in h1_body[1].text
    assert "ant" in h1_body[1].text
    for c in h1_body:
        assert c.token_count <= 20  # the body budget under this header

    # H2's own chunk must never carry H1's overlap tail across the
    # heading boundary -- each flush_pending() run is independent.
    assert len(h2_body) == 1
    assert "owl" not in h2_body[0].text
    assert "jar" in h2_body[0].text


# ---------------------------------------------------------------------------
# Tables: row-boundary splitting (Phase 4), header repeated on every split
# ---------------------------------------------------------------------------


def _table_unit(
    rows: tuple[tuple[str, ...], ...],
    *,
    header_row_count: int = 1,
    heading_path: tuple[str, ...] = ("H1",),
    caption: str | None = None,
) -> NormalizedUnit:
    return NormalizedUnit(
        kind="table",
        text="",
        heading_level=None,
        heading_path=heading_path,
        parent_index=0,
        page_start=1,
        page_end=1,
        table_rows=rows,
        header_row_count=header_row_count,
        caption=caption,
    )


def test_small_table_that_fits_max_tokens_stays_one_chunk() -> None:
    rows = (("Provider", "Max Retries"), ("Stripe", "5"), ("Adyen", "3"))
    units = [_heading("H1"), _table_unit(rows)]

    chunks = chunk_document(_doc(units))
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert tables[0].table_rows == rows
    assert tables[0].text == ""


def test_large_table_splits_by_row_boundaries_and_repeats_header_row() -> None:
    header = ("Server", "CPU", "RAM", "Status")
    data_rows = tuple((f"srv{i:02d}", "40%", "8GB", "healthy") for i in range(20))
    table_unit = _table_unit((header, *data_rows))
    config = ChunkingConfig(max_tokens=30, min_tokens=1, overlap_tokens=0, merge_peers=True)
    units = [_heading("H1"), table_unit]

    chunks = chunk_document(_doc(units), config=config)
    tables = [c for c in chunks if c.kind == "table"]

    assert len(tables) > 1
    for table_chunk in tables:
        # Never a mid-row cut, and the header block is repeated verbatim
        # at the top of every split -- each chunk is independently
        # interpretable per the plan's spec.
        assert table_chunk.table_rows[0] == header
        assert table_chunk.token_count <= config.max_tokens
        assert table_chunk.heading_path == ("H1",)
        assert table_chunk.page_start == 1 and table_chunk.page_end == 1

    # No data row is dropped or duplicated as data (only the header
    # legitimately repeats): every split's own non-header rows, laid end
    # to end, reconstruct exactly the original data rows in order.
    reconstructed = tuple(row for table_chunk in tables for row in table_chunk.table_rows[1:])
    assert reconstructed == data_rows

    # A term that only appears in a later split's rows (never in the
    # first split, never in the header) is still attached to a chunk that
    # carries the header -- i.e. header-repeat-on-split actually ran, not
    # just "chunking happened".
    last_row_chunk = next(c for c in tables if "srv19" in c.table_rows[-1])
    assert last_row_chunk.table_rows[0] == header
    assert last_row_chunk is not tables[0]


def test_table_oversized_single_cell_is_token_segmented_with_provenance() -> None:
    # Exact Tokenizer plan, Phase 3: a single data row whose one cell
    # alone exceeds the budget is no longer kept whole (which could still
    # overflow the model). It is token-split into fragments, each repeating
    # the column header and a "Row N" provenance marker, and each fitting
    # the exact payload budget.
    from ragmonk.tokenization.model_tokenizer import get_model_tokenizer

    big_cell = " ".join(f"cell{i:04d}" for i in range(100))
    table_unit = _table_unit((("Header",), (big_cell,)))
    config = ChunkingConfig(max_tokens=48, min_tokens=1, overlap_tokens=0, safety_tokens=0)
    units = [_heading("H1"), table_unit]

    chunks = chunk_document(_doc(units), config=config)
    tables = [c for c in chunks if c.kind == "table"]
    tok = get_model_tokenizer()
    bound = config.resolved_max_tokens - config.safety_tokens

    assert len(tables) > 1  # the oversized row was segmented
    for table_chunk in tables:
        assert tok.count(table_chunk.contextual_text, add_special_tokens=True) <= bound
        assert "Header" in table_chunk.contextual_text  # column name preserved
        assert "Row 1" in table_chunk.contextual_text  # row provenance preserved


def test_table_caption_repeated_in_every_split_chunk() -> None:
    header = ("Server", "CPU")
    data_rows = tuple((f"srv{i:02d}", "40%") for i in range(20))
    table_unit = _table_unit((header, *data_rows), caption="Table 1: Fleet status.")
    # Body budget ~18 (24 - safety 0 - special 2 - "Section: H1" 4): a
    # single "Server | CPU / srvNN | 40%" + caption row (15 tokens) fits,
    # so rows split at row boundaries -- not cell-segmented -- and the
    # caption is repeated verbatim on every chunk.
    config = ChunkingConfig(max_tokens=24, min_tokens=1, overlap_tokens=0, safety_tokens=0)
    units = [_heading("H1"), table_unit]

    chunks = chunk_document(_doc(units), config=config)
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) > 1
    for table_chunk in tables:
        assert table_chunk.caption == "Table 1: Fleet status."
        assert "Table 1: Fleet status." in table_chunk.contextual_text


def test_table_without_header_metadata_has_no_data_rows_to_split_into_zero_groups() -> None:
    # header_row_count spanning the whole table (a header-only table, no
    # data rows) can never be split -- there is nothing to split.
    units = [_heading("H1"), _table_unit((("Only", "Header"),), header_row_count=2)]
    config = ChunkingConfig(max_tokens=16, min_tokens=1, overlap_tokens=0, merge_peers=True)

    chunks = chunk_document(_doc(units), config=config)
    tables = [c for c in chunks if c.kind == "table"]
    assert len(tables) == 1
    assert tables[0].table_rows == (("Only", "Header"),)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_chunking_is_deterministic_across_repeated_calls() -> None:
    sentences = [f"Sentence {i} repeats reliably every run." for i in range(20)]
    units = [
        _heading("Doc"),
        _heading("H1", level=1, heading_path=("Doc",), parent_index=0),
        _paragraph(" ".join(sentences), heading_path=("Doc", "H1"), parent_index=1),
        _paragraph("short one.", heading_path=("Doc", "H1"), parent_index=1),
        _paragraph("short two.", heading_path=("Doc", "H1"), parent_index=1),
    ]
    config = ChunkingConfig(max_tokens=30, min_tokens=5, overlap_tokens=5, merge_peers=True)
    doc = _doc(units)

    first = chunk_document(doc, config=config)
    second = chunk_document(doc, config=config)

    assert first == second
    assert [c.text for c in first] == [c.text for c in second]


# ---------------------------------------------------------------------------
# Table captions (Docling structural association)
# ---------------------------------------------------------------------------


def _table_data() -> TableData:
    return TableData(
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
                text="alpha",
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


def test_table_caption_is_attached_to_the_table_chunk_not_a_stray_paragraph() -> None:
    doc = DoclingDocument(name="synthetic")
    caption_item = doc.add_text(DocItemLabel.CAPTION, "Table 1: Example numbers.")
    doc.add_table(_table_data(), caption=caption_item)

    normalized = normalizer.normalize(doc, DocumentFormat.MARKDOWN)
    chunks = chunk_document(normalized)

    assert [c.kind for c in chunks] == ["table"]
    table_chunk = chunks[0]
    assert table_chunk.caption == "Table 1: Example numbers."
    assert "Table 1: Example numbers." in table_chunk.contextual_text
    assert table_chunk.token_count == count_tokens(chunker._countable_text(normalized.units[0]))


def test_table_without_a_caption_has_none() -> None:
    doc = DoclingDocument(name="synthetic")
    doc.add_table(_table_data())

    normalized = normalizer.normalize(doc, DocumentFormat.MARKDOWN)
    chunks = chunk_document(normalized)

    assert chunks[0].caption is None


# ---------------------------------------------------------------------------
# Search Quality Improvement Plan, Phase 3: raw_text/search_text/
# embedding_text assembly (`text` is `raw_text`, unrenamed)
# ---------------------------------------------------------------------------


def test_search_text_and_contextual_text_assemble_title_heading_path_and_body() -> None:
    units = [
        _heading("Settlement Processing", heading_path=()),
        _heading(
            "Provider Settlement Flow",
            level=1,
            heading_path=("Settlement Processing",),
            parent_index=0,
        ),
        _paragraph(
            "The provider sends a settlement notification.",
            heading_path=("Settlement Processing", "Provider Settlement Flow"),
            parent_index=1,
        ),
    ]
    chunks = chunk_document(_doc(units), doc_title="Sportsbook Architecture")
    body = next(c for c in chunks if c.kind == "paragraph")

    # raw_text (`text`): the clean body, exactly as given -- never
    # rewritten with title/heading context.
    assert body.text == "The provider sends a settlement notification."

    # search_text: title, then each heading_path segment, then the body --
    # one per line, per the plan's own worked example.
    assert body.search_text == (
        "Sportsbook Architecture\n"
        "Settlement Processing\n"
        "Provider Settlement Flow\n"
        "The provider sends a settlement notification."
    )

    # embedding_text (`contextual_text`): a "Document: .../Section: ..."
    # breadcrumb, blank line, then the body.
    assert body.contextual_text == (
        "Document: Sportsbook Architecture\n"
        "Section: Settlement Processing > Provider Settlement Flow\n"
        "\n"
        "The provider sends a settlement notification."
    )


def test_search_text_and_contextual_text_degrade_gracefully_without_a_title() -> None:
    """``doc_title`` defaults to "" (unit tests building a synthetic
    document directly never pass one) -- both fields must still be well
    formed, just without a title line/segment.
    """
    units = [_heading("H1"), _paragraph("short body text.", heading_path=("H1",), parent_index=0)]
    chunks = chunk_document(_doc(units))
    body = next(c for c in chunks if c.kind == "paragraph")

    assert body.search_text == "H1\nshort body text."
    assert body.contextual_text == "Section: H1\n\nshort body text."


def test_search_text_and_contextual_text_degrade_to_plain_body_with_no_title_or_heading() -> None:
    units = [_paragraph("standalone body.", heading_path=())]
    chunks = chunk_document(_doc(units))
    assert chunks[0].search_text == "standalone body."
    assert chunks[0].contextual_text == "standalone body."


def test_heading_chunks_search_text_includes_ancestor_heading_path() -> None:
    """A HEADING-kind chunk's own `text` is just its own title (e.g.
    "Provider Settlement Flow") -- its `search_text` must still carry its
    *ancestor* heading_path and the document title, so a query for an
    ancestor heading term also finds a deeply nested heading row (see
    ``storage/repositories/documents_repo.py``'s module docstring on why
    this is what actually gets indexed for FTS).
    """
    units = [
        _heading("Settlement Processing", heading_path=()),
        _heading(
            "Provider Settlement Flow",
            level=1,
            heading_path=("Settlement Processing",),
            parent_index=0,
        ),
    ]
    chunks = chunk_document(_doc(units), doc_title="Sportsbook Architecture")
    nested_heading = chunks[1]
    assert nested_heading.text == "Provider Settlement Flow"
    assert nested_heading.search_text == (
        "Sportsbook Architecture\nSettlement Processing\nProvider Settlement Flow"
    )


# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------


def test_chunk_document_uses_default_config_when_none_passed() -> None:
    units = [_heading("H1"), _paragraph("short body text.", heading_path=("H1",), parent_index=0)]
    chunks = chunk_document(_doc(units))
    assert chunks[1].token_count <= ChunkingConfig().resolved_max_tokens
