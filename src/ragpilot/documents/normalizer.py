"""``DoclingDocument`` -> RAGpilot's normalized unit list.

Deliberately flat and index-addressed rather than a nested tree: a
``NormalizedUnit`` carries its own ``heading_path`` (ancestor titles,
root first) and ``parent_index`` (the position, in ``units``, of its
nearest enclosing heading), so nothing downstream needs to walk a tree to
recover a unit's provenance -- see the module docstring rationale on
``core.models.Section``.

Every format, PDF included, is normalized straight off the real
``DoclingDocument`` Docling itself produced (see ``docling_adapter.py``'s
module docstring) -- page numbers come from each item's own ``prov`` via
``_page_range``, and ``doc.num_pages()`` is always accurate, uniformly
across formats. Before Phase 1B (search-quality improvement plan), PDF
was normalized off a *second*, reparsed-from-Markdown document that
carried no ``prov`` at all, so this module reconstructed page numbers by
counting a page-break marker string embedded in that Markdown -- see git
history for that mechanism; nothing here depends on it anymore.

Search Quality Improvement Plan, Phase 2: a table's caption (Docling's
``TableItem.captions``, when the source document/backend populates it) is
resolved to plain text and attached to that table's own unit rather than
surviving as a separate, unrelated paragraph unit -- see
``_caption_by_table_ref``.

Search Quality Improvement Plan, Phase 5: ``normalize``'s ``is_scanned``
flag and ``docling_adapter``'s auto-OCR trigger both need to answer the
same question -- "does this PDF's extracted text look too sparse to be
real body content" -- so they share one definition,
``_is_low_text_density`` below, rather than drifting into two
independently-tuned heuristics. ``docling_adapter`` calls this module's
``normalize`` directly on its first, non-OCR conversion pass to decide
whether to re-run with OCR (see that module's ``_should_ocr``), and the
final document (OCR'd or not) is normalized again -- through this same
function -- for the document that actually gets chunked/indexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docling_core.types.doc.document import (
    DoclingDocument,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)

from ragpilot.core.models import DocumentFormat

# Furniture-like text Docling still emits as TextItem rows -- repeated per
# page, not real body content, so excluded from the normalized unit list.
_SKIPPED_TEXT_LABELS = {"page_header", "page_footer"}

# Search Quality Improvement Plan, Phase 5: below this many extracted
# characters per PDF page, a page's text is treated as effectively absent
# -- either genuinely scanned/image-only, or so sparse (a stray page
# number, a watermark) that it isn't real body content either way. A
# plain, tunable constant rather than a config field: the plan's own
# guidance is not to promote this into configuration until there is a
# real reason to, per-project, to tune it.
SCANNED_CHARS_PER_PAGE_THRESHOLD = 50


def _is_low_text_density(
    *, page_count: int | None, total_text_chars: int, pages_with_text: int
) -> bool:
    """True when a PDF's extracted text is sparse enough to look scanned/
    image-only: no text at all, well under
    ``SCANNED_CHARS_PER_PAGE_THRESHOLD`` characters per page on average,
    or most pages produced no text item at all. Shared by ``normalize``'s
    ``is_scanned`` flag and ``docling_adapter``'s auto-OCR trigger -- see
    this module's docstring for why there is only one such check, not
    two.
    """
    if not page_count:
        return total_text_chars == 0
    if total_text_chars == 0:
        return True
    if (total_text_chars / page_count) < SCANNED_CHARS_PER_PAGE_THRESHOLD:
        return True
    return pages_with_text * 2 < page_count


@dataclass(frozen=True)
class NormalizedUnit:
    kind: str  # "heading" | "paragraph" | "table"
    text: str
    heading_level: int | None
    heading_path: tuple[str, ...]
    parent_index: int | None
    page_start: int | None
    page_end: int | None
    table_rows: tuple[tuple[str, ...], ...] | None = None
    # A table's caption text, when Docling's own structure associates one
    # (``TableItem.captions`` -- a list of refs to a caption ``TextItem``
    # elsewhere in the tree, not necessarily adjacent in document order).
    # The caption's own ``TextItem`` is dropped from the unit list entirely
    # (see ``_consumed_caption_refs`` below) rather than also surviving as
    # an unrelated stray paragraph right after the table.
    caption: str | None = None


@dataclass(frozen=True)
class NormalizedDocument:
    title: str | None
    page_count: int | None
    is_scanned: bool
    units: list[NormalizedUnit] = field(default_factory=list)


def _label_value(item: TextItem) -> str:
    label = item.label
    return label.value if hasattr(label, "value") else str(label)


def _page_range(item: object) -> tuple[int | None, int | None]:
    prov = getattr(item, "prov", None) or []
    pages = [p.page_no for p in prov if getattr(p, "page_no", None) is not None]
    if not pages:
        return None, None
    return min(pages), max(pages)


def _caption_by_table_ref(doc: DoclingDocument) -> tuple[dict[str, str], set[str]]:
    """Pre-scans ``doc.tables`` for caption associations before the main
    per-item loop runs, so that loop can (a) attach a table's caption text
    to its own unit and (b) skip the caption's own ``TextItem`` wherever
    it appears in iteration order -- a table's caption is frequently *not*
    a sequential-order sibling of the table itself in Docling's tree, so
    this can't be decided item-by-item during a single forward pass.

    Returns ``(caption_text_by_table_self_ref, consumed_caption_self_refs)``.
    """
    caption_by_table: dict[str, str] = {}
    consumed: set[str] = set()
    for table in doc.tables:
        text = table.caption_text(doc).strip()
        if text:
            caption_by_table[table.self_ref] = text
        for cap_ref in table.captions:
            consumed.add(cap_ref.cref)
    return caption_by_table, consumed


def _table_rows(item: TableItem) -> tuple[tuple[str, ...], ...]:
    data = item.data
    grid: list[list[str]] = [["" for _ in range(data.num_cols)] for _ in range(data.num_rows)]
    for cell in data.table_cells:
        r, c = cell.start_row_offset_idx, cell.start_col_offset_idx
        if 0 <= r < data.num_rows and 0 <= c < data.num_cols:
            grid[r][c] = cell.text
    return tuple(tuple(row) for row in grid)


def normalize(doc: DoclingDocument, doc_format: DocumentFormat) -> NormalizedDocument:
    page_count = doc.num_pages() or None
    units: list[NormalizedUnit] = []
    # (heading_level, unit_index, title) for every heading currently "open"
    # -- a ``TitleItem`` is treated as level 0, ``SectionHeaderItem.level``
    # otherwise. Popped down to the nearest strictly-shallower heading
    # before each new heading/paragraph/table, exactly like a document
    # outline.
    stack: list[tuple[int, int, str]] = []
    total_text_chars = 0
    # Page numbers (1-based, Docling's own ``prov.page_no``) that produced
    # at least one non-empty heading/paragraph text item -- the "most
    # pages contain no text blocks" half of ``_is_low_text_density``.
    # Tables are deliberately not counted here, matching ``total_text_chars``
    # above: a table with OCR'd-garbage or empty cells shouldn't count as
    # "this page has text" any more than it already counts toward
    # ``total_text_chars``.
    pages_with_text: set[int] = set()
    caption_by_table, consumed_caption_refs = _caption_by_table_ref(doc)

    for item, _tree_level in doc.iterate_items():
        if not isinstance(item, TitleItem | SectionHeaderItem | TableItem | TextItem):
            continue
        if isinstance(item, TextItem) and _label_value(item) in _SKIPPED_TEXT_LABELS:
            continue
        if isinstance(item, TextItem) and item.self_ref in consumed_caption_refs:
            # Already surfaced as this table's own `caption` field below --
            # would otherwise also survive here as an unrelated stray
            # paragraph, per this module's docstring.
            continue

        page_start, page_end = _page_range(item)

        if isinstance(item, TitleItem | SectionHeaderItem):
            level = 0 if isinstance(item, TitleItem) else item.level
            while stack and stack[-1][0] >= level:
                stack.pop()
            heading_path = tuple(title for _, _, title in stack)
            parent_index = stack[-1][1] if stack else None
            index = len(units)
            units.append(
                NormalizedUnit(
                    kind="heading",
                    text=item.text,
                    heading_level=level,
                    heading_path=heading_path,
                    parent_index=parent_index,
                    page_start=page_start,
                    page_end=page_end,
                )
            )
            stack.append((level, index, item.text))
            total_text_chars += len(item.text)
            if item.text.strip() and page_start is not None and page_end is not None:
                pages_with_text.update(range(page_start, page_end + 1))
            continue

        heading_path = tuple(title for _, _, title in stack)
        parent_index = stack[-1][1] if stack else None

        if isinstance(item, TableItem):
            units.append(
                NormalizedUnit(
                    kind="table",
                    text="",
                    heading_level=None,
                    heading_path=heading_path,
                    parent_index=parent_index,
                    page_start=page_start,
                    page_end=page_end,
                    table_rows=_table_rows(item),
                    caption=caption_by_table.get(item.self_ref),
                )
            )
            continue

        text = item.text.strip()
        if not text:
            continue
        total_text_chars += len(text)
        if page_start is not None and page_end is not None:
            pages_with_text.update(range(page_start, page_end + 1))
        units.append(
            NormalizedUnit(
                kind="paragraph",
                text=text,
                heading_level=None,
                heading_path=heading_path,
                parent_index=parent_index,
                page_start=page_start,
                page_end=page_end,
            )
        )

    title = next(
        (u.text for u in units if u.kind == "heading" and u.heading_level == 0), None
    )
    if title is None:
        title = next((u.text for u in units if u.kind == "heading"), None)

    # Docling does not itself flag a PDF page as scanned/image-only.
    # ``_is_low_text_density`` is the same low-density check
    # ``docling_adapter``'s auto-OCR trigger runs on the plain (pre-OCR)
    # conversion -- calling it again here, on whatever document actually
    # got normalized (OCR'd or not, depending on ``documents.ocr``), means
    # ``is_scanned`` reports "this document still looks textless" even for
    # a document OCR was tried on and didn't help, not just "OCR was never
    # attempted".
    is_scanned = doc_format is DocumentFormat.PDF and bool(page_count) and _is_low_text_density(
        page_count=page_count,
        total_text_chars=total_text_chars,
        pages_with_text=len(pages_with_text),
    )

    return NormalizedDocument(
        title=title, page_count=page_count, is_scanned=is_scanned, units=units
    )
