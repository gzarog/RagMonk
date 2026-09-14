"""``DoclingDocument`` -> RAGpilot's normalized unit list.

Deliberately flat and index-addressed rather than a nested tree: a
``NormalizedUnit`` carries its own ``heading_path`` (ancestor titles,
root first) and ``parent_index`` (the position, in ``units``, of its
nearest enclosing heading), so nothing downstream needs to walk a tree to
recover a unit's provenance -- see the module docstring rationale on
``core.models.Section``.

For PDF, ``docling_adapter.convert`` hands back a document reparsed from
its own Markdown export rather than Docling's original PDF-layout
document (see that module's docstring) -- every item in it carries an
empty ``prov`` list, so page numbers can't be read off items the way
``_page_range`` does for every other format. Instead, PDF's Markdown
export embeds a literal page-break marker string at each page transition;
``normalize`` accepts that marker plus the real page count as optional
overrides and reconstructs page numbers by counting marker crossings
while walking the document, dropping the marker items themselves from
the output.

Search Quality Improvement Plan, Phase 2: a table's caption (Docling's
``TableItem.captions``, when the source document/backend populates it) is
resolved to plain text and attached to that table's own unit rather than
surviving as a separate, unrelated paragraph unit -- see
``_caption_by_table_ref``.
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


def normalize(
    doc: DoclingDocument,
    doc_format: DocumentFormat,
    *,
    page_count_override: int | None = None,
    page_break_marker: str | None = None,
) -> NormalizedDocument:
    page_count = (
        page_count_override if page_count_override is not None else (doc.num_pages() or None)
    )
    units: list[NormalizedUnit] = []
    # (heading_level, unit_index, title) for every heading currently "open"
    # -- a ``TitleItem`` is treated as level 0, ``SectionHeaderItem.level``
    # otherwise. Popped down to the nearest strictly-shallower heading
    # before each new heading/paragraph/table, exactly like a document
    # outline.
    stack: list[tuple[int, int, str]] = []
    total_text_chars = 0
    # Only meaningful when page_break_marker is set (PDF): the page
    # everything encountered so far belongs to, incremented each time a
    # marker item is crossed.
    current_page = 1
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

        is_marker = (
            page_break_marker is not None
            and isinstance(item, TextItem)
            and item.text == page_break_marker
        )
        if is_marker:
            # A page-transition marker, not real content -- never becomes
            # a unit, just advances the page counter everything after it
            # is stamped with.
            current_page += 1
            continue

        page_start: int | None
        page_end: int | None
        if page_break_marker is not None:
            # Reparsed-from-Markdown items carry no ``prov`` at all, so
            # ``_page_range`` would just return (None, None) here --
            # ``current_page`` (tracked via marker crossings above) is
            # this format's only source of page numbers.
            page_start, page_end = current_page, current_page
        else:
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
                    caption=caption_by_table.get(item.self_ref),
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
