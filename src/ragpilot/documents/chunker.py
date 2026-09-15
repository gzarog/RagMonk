"""Groups a ``NormalizedDocument``'s units into indexable/storable chunks.

Search Quality Improvement Plan, Phase 3: every chunk now exposes three
distinct text views, computed once here rather than re-derived ad hoc by
each consumer:

- ``text`` (unchanged) -- the clean, authoritative body shown to users as
  evidence. Never rewritten with title/heading context baked in.
- ``search_text`` -- lexical text for FTS: the document title, then each
  ``heading_path`` segment, then ``text``, one per line. Boosts matching
  on title/heading vocabulary a query didn't phrase against the raw body
  (see ``_search_text``), without polluting the clean ``text`` returned
  as evidence.
- ``contextual_text`` -- this chunk's embedding input: a
  "Document: .../Section: ..." breadcrumb followed by ``text`` (see
  ``_contextual_text``). Phase 2 first introduced this field from
  ``heading_path`` alone, before the document's title was available this
  early in the pipeline (it used to be assembled in ``pipeline.py`` only
  after chunking); Phase 3 threads ``doc_title`` into ``chunk_document``
  itself so both fields can include it.

Both are plain string formatting over already-known values (no new
dependency), computed alongside ``token_count`` so a caller
(``documents/pipeline.py``) never has to re-walk a chunk's text/heading
path/title to build them itself.

Search Quality Improvement Plan, Phase 2: chunk boundaries are now
token-aware and hierarchy-aware (``core.config.ChunkingConfig``) rather
than the previous pure character-count paragraph grouping. Headings and
tables are still stored one-to-one (never merged away, never split) --
only *how many pieces a run of paragraph units under one heading becomes*
changed:

- Consecutive paragraph units under the same heading are greedily packed
  into chunks up to ``max_tokens`` (replacing the old flat
  ``max_chunk_chars``), using ``documents/tokenization.py``'s
  dependency-free token estimator -- see that module's docstring for why
  it isn't the real embedding-model tokenizer.
- A single unit whose own text exceeds ``max_tokens`` is split at
  sentence, then word, boundaries (never a raw character cut) before
  packing -- see ``tokenization.split_by_token_budget``.
- ``overlap_tokens`` worth of trailing text from one chunk seeds the next
  chunk *within the same heading's pending run only*: ``flush_pending``
  is always scoped to one contiguous run of paragraph units between two
  headings/tables, so overlap structurally cannot bleed across a heading
  boundary -- there is no shared state between two separate
  ``flush_pending`` calls.
- ``merge_peers`` absorbs a chunk that came out under ``min_tokens`` into
  an adjacent chunk from the *same* run, provided the merge still fits
  ``max_tokens`` -- avoiding a standalone near-empty chunk without ever
  exceeding the hard ceiling.
- A table whose row-aware rendering (``table_renderer.render_table``)
  exceeds ``max_tokens`` is split at row boundaries -- never mid-row --
  into multiple table chunks, each repeating the table's header rows at
  the top (``table_renderer.split_data_rows``), so every resulting chunk
  stays independently interpretable. A table that already fits
  ``max_tokens``, or whose header block leaves no data rows to split, is
  still emitted as exactly one chunk, same as before. The one remaining
  atomic exception is a single row that alone (with its header repeated)
  still exceeds ``max_tokens`` -- there is no meaningful sub-row unit to
  cut at, so it is kept whole -- see ``split_data_rows``'s docstring.
  Before Phase 4, a table was *never* split regardless of size; see git
  history for that simpler, whole-table-only behavior.

Every output ``Chunk`` still carries its own page/heading-path
provenance, per the blueprint's evidence-first rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from ragpilot.core.config import ChunkingConfig
from ragpilot.documents import table_renderer
from ragpilot.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragpilot.documents.tokenization import count_tokens, split_by_token_budget


@dataclass(frozen=True)
class Chunk:
    kind: str  # "heading" | "paragraph" | "table"
    text: str
    heading_level: int | None
    heading_path: tuple[str, ...]
    parent_index: int | None  # index into the returned chunk list, not the source units list
    page_start: int | None
    page_end: int | None
    table_rows: tuple[tuple[str, ...], ...] | None = None
    # A table's associated caption text (see normalizer._caption_by_table_ref);
    # always None for "heading"/"paragraph" chunks.
    caption: str | None = None
    # This chunk's embedding input: "Document: <title>" / "Section: <heading
    # path>" breadcrumb followed by `text` (or, for a table, its
    # caption/row-aware rendering -- see `_countable_text`). See `_contextual_text`.
    contextual_text: str = ""
    # This chunk's FTS input: document title, then each heading_path segment,
    # then `text` -- one per line. See `_search_text`.
    search_text: str = ""
    # Real (estimated) subword-token count of the text `token_count` was
    # computed from -- see `tokenization.count_tokens`. A table's count is
    # taken from its row-aware rendering (see `_countable_text`), which is
    # exactly what its own `table_rows` (plus `caption`) render to -- its
    # `text` field is always "".
    token_count: int = 0


@dataclass(frozen=True)
class _Piece:
    """One already-budget-sized fragment of a paragraph unit's text, with
    that unit's page provenance carried along -- page numbers are tracked
    per source ``NormalizedUnit``, not per word, so every piece split out
    of one unit shares that unit's page_start/page_end.
    """

    text: str
    page_start: int | None
    page_end: int | None


def _contextual_text(doc_title: str, heading_path: tuple[str, ...], body: str) -> str:
    """This chunk's embedding input: a breadcrumb of where the text sits
    in the document, then a blank line, then the text itself -- giving a
    similarity model context an isolated chunk's own words don't carry
    (e.g. "Settlement" alone doesn't say *which* settlement flow), per
    the search-quality plan's own worked example. Either header line is
    dropped when its source value is empty, so a title-less document or a
    heading-less unit degrades to just the other line, and a chunk with
    neither degrades to plain `body`.
    """
    header_lines = []
    if doc_title:
        header_lines.append(f"Document: {doc_title}")
    if heading_path:
        header_lines.append(f"Section: {' > '.join(heading_path)}")
    header = "\n".join(header_lines)
    if not header:
        return body
    return f"{header}\n\n{body}" if body else header


def _search_text(doc_title: str, heading_path: tuple[str, ...], body: str) -> str:
    """This chunk's FTS input: the document title and every heading_path
    segment each get their own line ahead of the body -- so a query
    phrased against title/heading vocabulary that never appears in the
    body text itself still matches this chunk on the lexical pass,
    without ever changing `text`, the clean body returned as evidence.
    """
    header_lines = [line for line in (doc_title, *heading_path) if line]
    if not body:
        return "\n".join(header_lines) if header_lines else body
    return "\n".join((*header_lines, body)) if header_lines else body


def _countable_text(unit: NormalizedUnit) -> str:
    """The text a table unit's ``token_count``/``contextual_text`` should
    be computed from -- its own ``text`` is always ``""`` (cells live in
    ``table_rows``), so this renders its full grid row-aware (see
    ``table_renderer.render_table``, and that module's docstring for why
    that's not the same as the pre-Phase-4 flattened-cell blob), plus the
    caption when one is attached.
    """
    if unit.kind != "table":
        return unit.text
    return table_renderer.render_table(unit.table_rows or (), caption=unit.caption)


def _pieces_for_unit(unit: NormalizedUnit, max_tokens: int) -> list[_Piece]:
    return [
        _Piece(text=t, page_start=unit.page_start, page_end=unit.page_end)
        for t in split_by_token_budget(unit.text, max_tokens)
    ]


def _pack_pieces(pieces: list[_Piece], config: ChunkingConfig) -> list[list[_Piece]]:
    """Greedy token-budget packing across every piece in one heading's
    pending run, applying ``overlap_tokens`` between consecutive groups.

    Every piece is individually <= ``max_tokens`` (guaranteed by
    ``split_by_token_budget``), so a fresh group can always accept at
    least one new piece -- the overlap seed is trimmed, never the new
    piece, so a produced group never exceeds ``max_tokens``.
    """
    groups: list[list[_Piece]] = []
    overlap_seed: list[_Piece] = []
    i = 0
    n = len(pieces)
    while i < n:
        current: list[_Piece] = []
        current_tokens = 0
        if overlap_seed and config.overlap_tokens > 0:
            next_tokens = count_tokens(pieces[i].text)
            seed: list[_Piece] = []
            seed_tokens = 0
            for piece in reversed(overlap_seed):
                piece_tokens = count_tokens(piece.text)
                if seed_tokens + piece_tokens > config.overlap_tokens:
                    break
                if seed_tokens + piece_tokens + next_tokens > config.max_tokens:
                    break
                seed.insert(0, piece)
                seed_tokens += piece_tokens
            current, current_tokens = seed, seed_tokens

        started_new = False
        while i < n:
            piece = pieces[i]
            piece_tokens = count_tokens(piece.text)
            if current and started_new and current_tokens + piece_tokens > config.max_tokens:
                break
            current.append(piece)
            current_tokens += piece_tokens
            started_new = True
            i += 1
            if current_tokens >= config.max_tokens:
                break

        groups.append(current)
        overlap_seed = current
    return groups


def _group_tokens(group: list[_Piece]) -> int:
    return sum(count_tokens(p.text) for p in group)


def _split_evenly(
    pieces: list[_Piece], max_tokens: int
) -> tuple[list[_Piece], list[_Piece]]:
    """Splits ``pieces`` into two halves as close to equal token size as
    piece boundaries allow -- used to rebalance two adjacent packed
    groups rather than just re-testing their original split point (see
    ``_merge_peers``).
    """
    target = -(-_group_tokens(pieces) // 2)  # ceil(total / 2)
    first: list[_Piece] = []
    first_tokens = 0
    split_at = len(pieces)
    for idx, piece in enumerate(pieces):
        piece_tokens = count_tokens(piece.text)
        if first and first_tokens + piece_tokens > target:
            split_at = idx
            break
        first.append(piece)
        first_tokens += piece_tokens
    return first, pieces[split_at:]


def _merge_peers(groups: list[list[_Piece]], config: ChunkingConfig) -> list[list[_Piece]]:
    """Rebalances each adjacent pair of packed groups, left to right,
    whenever either side is under ``min_tokens``.

    A greedily-packed group can end up short of the floor purely because
    content didn't divide evenly across ``max_tokens``-sized groups -- and
    critically, it can *never* be fixed by simply concatenating it with
    its immediate neighbor: ``_pack_pieces`` only starts a new group when
    the current one plus the very next piece would exceed ``max_tokens``,
    so any two adjacent groups it produces are, by that same construction,
    already too large to recombine. Splitting their *combined* pieces
    evenly instead (rather than at the original boundary) can still bring
    both sides up to ``min_tokens`` when the pair's total supports it; when
    it doesn't (not enough combined content for two full-sized floors --
    ``min_tokens`` is a soft target, not a hard floor, see
    ``ChunkingConfig``), the pair is left as ``_pack_pieces`` produced it.
    Every rebalanced half is re-checked against ``max_tokens`` before
    being accepted, so the one hard ceiling can never regress either.
    """
    if not config.merge_peers or len(groups) < 2:
        return groups
    result = [list(g) for g in groups]
    for i in range(len(result) - 1):
        if _group_tokens(result[i]) >= config.min_tokens and (
            _group_tokens(result[i + 1]) >= config.min_tokens
        ):
            continue
        first, second = _split_evenly(result[i] + result[i + 1], config.max_tokens)
        if not first or not second:
            continue
        if _group_tokens(first) <= config.max_tokens and _group_tokens(second) <= config.max_tokens:
            result[i], result[i + 1] = first, second
    return result


def _finalize_group(
    group: list[_Piece],
    heading_path: tuple[str, ...],
    parent_index: int | None,
    doc_title: str,
) -> Chunk:
    text = "\n\n".join(p.text for p in group)
    pages = [pg for p in group for pg in (p.page_start, p.page_end) if pg is not None]
    return Chunk(
        kind="paragraph",
        text=text,
        heading_level=None,
        heading_path=heading_path,
        parent_index=parent_index,
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        contextual_text=_contextual_text(doc_title, heading_path, text),
        search_text=_search_text(doc_title, heading_path, text),
        token_count=count_tokens(text),
    )


def _table_chunk(
    unit: NormalizedUnit, rows: tuple[tuple[str, ...], ...], parent_index: int | None
) -> Chunk:
    rendered = table_renderer.render_table(rows, caption=unit.caption)
    return Chunk(
        kind="table",
        text="",
        heading_level=None,
        heading_path=unit.heading_path,
        parent_index=parent_index,
        page_start=unit.page_start,
        page_end=unit.page_end,
        table_rows=rows,
        caption=unit.caption,
        contextual_text=_contextual_text(unit.heading_path, rendered),
        token_count=count_tokens(rendered),
    )


def _table_chunks(
    unit: NormalizedUnit, parent_index: int | None, cfg: ChunkingConfig
) -> list[Chunk]:
    """One chunk for a table whose row-aware rendering already fits
    ``max_tokens``; multiple row-boundary-split chunks, header rows
    repeated at the top of each, for one that doesn't -- see this
    module's docstring and ``table_renderer.split_data_rows``.
    """
    rows = unit.table_rows or ()
    whole = _table_chunk(unit, rows, parent_index)
    if whole.token_count <= cfg.max_tokens or not rows:
        return [whole]

    header_rows = rows[: unit.header_row_count]
    data_rows = rows[unit.header_row_count :]
    groups = table_renderer.split_data_rows(
        data_rows,
        header_rows=header_rows,
        max_tokens=cfg.max_tokens,
        fixed_overhead=unit.caption or "",
    )
    if len(groups) <= 1:
        # Splitting couldn't actually reduce anything -- no data rows
        # beyond the header, or the header alone (or one oversized row)
        # already exceeds the budget -- so the single whole-table chunk
        # already computed above is exactly what a one-group split would
        # produce; reuse it rather than rebuilding an identical Chunk.
        return [whole]

    return [
        _table_chunk(unit, header_rows + tuple(tuple(row) for row in group), parent_index)
        for group in groups
    ]


def chunk_document(
    normalized: NormalizedDocument,
    *,
    config: ChunkingConfig | None = None,
    doc_title: str = "",
) -> list[Chunk]:
    """``doc_title`` (the containing document's title, once known --
    ``documents/metadata.py``'s ``extract_metadata`` output) is threaded
    into every chunk's ``search_text``/``contextual_text``; omit it (the
    default) when a caller genuinely has no title yet, e.g. a unit test
    building a synthetic ``NormalizedDocument`` directly -- both fields
    then simply degrade to heading-path-only, as Phase 2 first shipped
    them.
    """
    cfg = config or ChunkingConfig()

    # Headings are never merged away, but merging/splitting paragraphs
    # does change row counts, so a heading's position in ``units`` no
    # longer matches its position in the output -- this remaps old index
    # -> new index for every heading so paragraph/table ``parent_index``
    # values stay valid.
    old_heading_to_new: dict[int, int] = {}
    chunks: list[Chunk] = []
    pending: list[NormalizedUnit] = []

    def flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        first = pending[0]
        parent_new = (
            old_heading_to_new.get(first.parent_index) if first.parent_index is not None else None
        )
        pieces: list[_Piece] = []
        for unit in pending:
            pieces.extend(_pieces_for_unit(unit, cfg.max_tokens))
        groups = _merge_peers(_pack_pieces(pieces, cfg), cfg)
        for group in groups:
            if group:
                chunks.append(_finalize_group(group, first.heading_path, parent_new, doc_title))
        pending = []

    for old_index, unit in enumerate(normalized.units):
        if unit.kind == "paragraph":
            pending.append(unit)
            continue

        flush_pending()
        parent_new = (
            old_heading_to_new.get(unit.parent_index) if unit.parent_index is not None else None
        )

        if unit.kind == "table":
            chunks.extend(_table_chunks(unit, parent_new, cfg))
            continue

        countable = _countable_text(unit)
        new_index = len(chunks)
        chunks.append(
            Chunk(
                kind=unit.kind,
                text=unit.text,
                heading_level=unit.heading_level,
                heading_path=unit.heading_path,
                parent_index=parent_new,
                page_start=unit.page_start,
                page_end=unit.page_end,
                table_rows=unit.table_rows,
                caption=unit.caption,
                contextual_text=_contextual_text(doc_title, unit.heading_path, countable),
                search_text=_search_text(doc_title, unit.heading_path, countable),
                token_count=count_tokens(countable),
            )
        )
        old_heading_to_new[old_index] = new_index

    flush_pending()
    return chunks
