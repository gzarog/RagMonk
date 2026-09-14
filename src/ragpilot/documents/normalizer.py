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

    for item, _tree_level in doc.iterate_items():
        if not isinstance(item, TitleItem | SectionHeaderItem | TableItem | TextItem):
            continue
        if isinstance(item, TextItem) and _label_value(item) in _SKIPPED_TEXT_LABELS:
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
                )
            )
            continue

        text = item.text.strip()
        if not text:
            continue
        total_text_chars += len(text)
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

    # Docling does not itself flag a PDF page as scanned/image-only (OCR is
    # out of scope here regardless -- see docling_adapter.py). With OCR
    # off, a page rendered from an image-only PDF simply yields zero text
    # items, so "every page produced no text at all" is a reasonable,
    # Docling-observable proxy for "this looks scanned" without running OCR.
    is_scanned = doc_format is DocumentFormat.PDF and bool(page_count) and total_text_chars == 0

    return NormalizedDocument(
        title=title, page_count=page_count, is_scanned=is_scanned, units=units
    )
