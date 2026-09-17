"""Groups a ``NormalizedDocument``'s units into indexable/storable chunks.

Exact Tokenizer plan, Phase 2: chunk boundaries are budgeted against the
**exact** tokenizer of the embedding model
(``ragmonk.tokenization.model_tokenizer``) and against the **full
contextual payload** the embedder actually sees -- the document-title
prefix, the section/heading breadcrumb, the separators, the chunk body,
and the model's own special tokens -- not just the raw body. The
guaranteed invariant for every embeddable chunk is::

    exact_tokens(contextual_text, with special tokens)
        <= resolved_max_tokens - safety_tokens

so no normal embedding request relies on the model silently truncating an
over-long input. The budget arithmetic is exact because the WordPiece
tokenizer pre-splits on whitespace: inter-piece separators (spaces,
newlines) cost zero tokens and whitespace-joined segments are additive,
so ``special + header + body`` equals the real payload length (see
``documents/tokenization.py``).

Three text views per chunk, computed once here (unchanged from the
Search-Quality design):

- ``text`` -- the clean, authoritative body shown to users as evidence.
- ``search_text`` -- lexical text for FTS: title, then heading_path
  segments, then ``text``, one per line (see ``_search_text``).
- ``contextual_text`` -- this chunk's embedding input: a
  "Document: .../Section: ..." breadcrumb (possibly reduced -- see
  ``_fit_header``) followed by ``text`` (see ``_contextual_text``).

Structure preserved from the previous chunker: headings and tables are
still stored one-to-one (never merged away); consecutive paragraph units
under one heading are greedily packed up to the per-run body budget;
``overlap_tokens`` of trailing text seeds the next chunk within the same
heading run only; ``merge_peers`` absorbs an under-``min_tokens`` chunk
into an adjacent same-run peer; and a table whose rendering exceeds the
budget is split at row boundaries with header rows repeated. Every output
``Chunk`` still carries its own page/heading-path provenance.

Long contextual headers use a deterministic reduction policy
(``_fit_header``): keep the deepest/current heading, then the title,
dropping oldest intermediate ancestors first, and only token-truncating
an individually oversized heading/title as the final fallback -- the
evidence body is never truncated merely to keep a long breadcrumb.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ragmonk.core.config import ChunkingConfig
from ragmonk.documents import table_renderer
from ragmonk.documents.normalizer import NormalizedDocument, NormalizedUnit
from ragmonk.documents.tokenization import count_tokens, split_by_token_budget
from ragmonk.tokenization.model_tokenizer import ModelTokenizer, get_model_tokenizer

# Exact Tokenizer plan: the chunker now budgets the exact contextual
# payload, so both version axes advance to "2". ``CHUNKER_VERSION`` gates
# chunk boundaries/``search_text`` (bumped: boundaries are now exact-token
# and payload-aware); ``EMBEDDING_TEXT_VERSION`` gates ``contextual_text``
# assembly (bumped: the header may now be deterministically reduced to fit
# the model budget). See ``documents/pipeline.py``'s
# ``document_version_stamp`` and ``indexing/incremental.decide_reprocessing``.
CHUNKER_VERSION = "2"
EMBEDDING_TEXT_VERSION = "2"

# A callable that builds a chunk's ``contextual_text`` from its heading
# path, body and kind (fitting/reducing the header and recording payload
# diagnostics). Defined once per ``chunk_document`` call.
_MakeContextual = Callable[[tuple[str, ...], str, str], str]


@dataclass
class ChunkingDiagnostics:
    """Per-run counters the chunker fills when the caller passes one.

    Surfaced by ``ragmonk doctor``/``status`` (Phase 5). ``max_payload_tokens``
    is the largest exact contextual-payload size (with special tokens)
    observed across all emitted chunks -- it must always stay at or below
    ``resolved_max_tokens - safety_tokens``.
    """

    chunks_split_by_budget: int = 0
    context_headers_reduced: int = 0
    oversized_table_rows: int = 0
    oversized_table_cells: int = 0
    max_payload_tokens: int = 0
    reductions_by_kind: dict[str, int] = field(default_factory=dict)


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
    # path>" breadcrumb (possibly reduced) followed by `text` (or, for a
    # table, its caption/row-aware rendering). See `_contextual_text`.
    contextual_text: str = ""
    # This chunk's FTS input: document title, then each heading_path segment,
    # then `text` -- one per line. See `_search_text`.
    search_text: str = ""
    # Exact body-token count of the text `token_count` was computed from
    # (no special tokens) -- see `tokenization.count_tokens`. A table's
    # count is taken from its row-aware rendering; its `text` is always "".
    token_count: int = 0


@dataclass(frozen=True)
class _Piece:
    """One already-budget-sized fragment of a paragraph unit's text, with
    that unit's page provenance carried along -- page numbers are tracked
    per source ``NormalizedUnit``, not per word.
    """

    text: str
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True)
class _Budget:
    """The exact per-heading-run body budget and the packing knobs derived
    from the model ceiling, the safety reserve and the (fitted) header.
    """

    body_max: int  # max body tokens so special + header + body <= payload budget
    overlap: int
    min_tokens: int
    merge_peers: bool


def _header_text(doc_title: str, heading_segments: tuple[str, ...]) -> str:
    """The breadcrumb header portion of a contextual payload (no body)."""
    header_lines = []
    if doc_title:
        header_lines.append(f"Document: {doc_title}")
    if heading_segments:
        header_lines.append(f"Section: {' > '.join(heading_segments)}")
    return "\n".join(header_lines)


def _contextual_text(header: str, body: str) -> str:
    """Assembles the embedding input from an already-fitted ``header`` and
    the chunk ``body``. Either being empty degrades gracefully to the
    other.
    """
    if not header:
        return body
    return f"{header}\n\n{body}" if body else header


def _search_text(doc_title: str, heading_path: tuple[str, ...], body: str) -> str:
    """This chunk's FTS input: the document title and every heading_path
    segment each get their own line ahead of the body.
    """
    header_lines = [line for line in (doc_title, *heading_path) if line]
    if not body:
        return "\n".join(header_lines) if header_lines else body
    return "\n".join((*header_lines, body)) if header_lines else body


def _truncate_to_tokens(tokenizer: ModelTokenizer, text: str, budget: int) -> str:
    """Returns the longest token-boundary prefix of ``text`` fitting
    ``budget`` body tokens (>=1). Used only as the header-reduction final
    fallback for an individually oversized heading/title.
    """
    if budget <= 0:
        return ""
    if tokenizer.count(text, add_special_tokens=False) <= budget:
        return text
    return tokenizer.split(text, budget)[0]


def _fit_header(
    tokenizer: ModelTokenizer,
    doc_title: str,
    heading_path: tuple[str, ...],
    header_budget: int,
) -> tuple[str, bool]:
    """Reduces the breadcrumb so its exact token count fits ``header_budget``.

    Deterministic policy (Exact Tokenizer plan, Phase 2):
      1. Always keep the deepest/current heading.
      2. Keep the document title when the result still fits.
      3. Drop oldest intermediate ancestors first.
      4. Token-truncate an individually oversized title/heading only as a
         final fallback.

    Returns ``(header_text, reduced)`` where ``reduced`` is True if any
    step past the untouched full header was needed.
    """
    segments = tuple(heading_path)

    def fits(title: str, segs: tuple[str, ...]) -> bool:
        return tokenizer.count(_header_text(title, segs), add_special_tokens=False) <= header_budget

    if fits(doc_title, segments):
        return _header_text(doc_title, segments), False

    # 3. Drop oldest intermediate ancestors first, keeping the deepest.
    if len(segments) > 1:
        for drop in range(1, len(segments)):
            candidate = segments[drop:]
            if fits(doc_title, candidate):
                return _header_text(doc_title, candidate), True
        segments = segments[-1:]  # only the deepest heading survives

    # 4a. Deepest heading + title still too big -> drop the title.
    if doc_title and fits("", segments):
        return _header_text("", segments), True

    # 4b. Final fallback: token-truncate the (single, oversized) deepest
    # heading so the "Section: ..." line fits on its own.
    if segments:
        prefix_cost = tokenizer.count("Section: ", add_special_tokens=False)
        truncated = _truncate_to_tokens(tokenizer, segments[0], max(1, header_budget - prefix_cost))
        return _header_text("", (truncated,)), True

    # No heading at all: truncate the title itself as the last resort.
    if doc_title:
        prefix_cost = tokenizer.count("Document: ", add_special_tokens=False)
        truncated = _truncate_to_tokens(tokenizer, doc_title, max(1, header_budget - prefix_cost))
        return _header_text(truncated, ()), True
    return "", False


def _countable_text(unit: NormalizedUnit) -> str:
    """The text a table unit's counts should be computed from -- its own
    ``text`` is always ``""``, so this renders its grid row-aware plus the
    caption. Non-table units return their own ``text``.
    """
    if unit.kind != "table":
        return unit.text
    return table_renderer.render_table(unit.table_rows or (), caption=unit.caption)


def _pieces_for_unit(unit: NormalizedUnit, body_max: int) -> list[_Piece]:
    return [
        _Piece(text=t, page_start=unit.page_start, page_end=unit.page_end)
        for t in split_by_token_budget(unit.text, body_max)
    ]


def _pack_pieces(pieces: list[_Piece], budget: _Budget) -> list[list[_Piece]]:
    """Greedy body-token packing across one heading run's pieces, applying
    ``budget.overlap`` between consecutive groups. Every piece is
    individually <= ``budget.body_max`` (guaranteed by
    ``split_by_token_budget``), and the overlap seed is trimmed (never the
    new piece), so a produced group never exceeds ``body_max``.
    """
    groups: list[list[_Piece]] = []
    overlap_seed: list[_Piece] = []
    i = 0
    n = len(pieces)
    while i < n:
        current: list[_Piece] = []
        current_tokens = 0
        if overlap_seed and budget.overlap > 0:
            next_tokens = count_tokens(pieces[i].text)
            seed: list[_Piece] = []
            seed_tokens = 0
            for piece in reversed(overlap_seed):
                piece_tokens = count_tokens(piece.text)
                if seed_tokens + piece_tokens > budget.overlap:
                    break
                if seed_tokens + piece_tokens + next_tokens > budget.body_max:
                    break
                seed.insert(0, piece)
                seed_tokens += piece_tokens
            current, current_tokens = seed, seed_tokens

        started_new = False
        while i < n:
            piece = pieces[i]
            piece_tokens = count_tokens(piece.text)
            if current and started_new and current_tokens + piece_tokens > budget.body_max:
                break
            current.append(piece)
            current_tokens += piece_tokens
            started_new = True
            i += 1
            if current_tokens >= budget.body_max:
                break

        groups.append(current)
        overlap_seed = current
    return groups


def _group_tokens(group: list[_Piece]) -> int:
    return sum(count_tokens(p.text) for p in group)


def _split_evenly(pieces: list[_Piece], body_max: int) -> tuple[list[_Piece], list[_Piece]]:
    """Splits ``pieces`` into two halves as close to equal token size as
    piece boundaries allow -- used to rebalance two adjacent packed groups.
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


def _merge_peers(groups: list[list[_Piece]], budget: _Budget) -> list[list[_Piece]]:
    """Rebalances each adjacent pair of packed groups, left to right,
    whenever either side is under ``budget.min_tokens`` -- see git history
    and the Search-Quality design for the full rationale. Every rebalanced
    half is re-checked against ``body_max`` so the hard ceiling never
    regresses.
    """
    if not budget.merge_peers or len(groups) < 2:
        return groups
    result = [list(g) for g in groups]
    for i in range(len(result) - 1):
        if _group_tokens(result[i]) >= budget.min_tokens and (
            _group_tokens(result[i + 1]) >= budget.min_tokens
        ):
            continue
        first, second = _split_evenly(result[i] + result[i + 1], budget.body_max)
        if not first or not second:
            continue
        if _group_tokens(first) <= budget.body_max and _group_tokens(second) <= budget.body_max:
            result[i], result[i + 1] = first, second
    return result


class _RunBudgeter:
    """Resolves the exact per-run body budget and fitted header for a given
    heading path, given the model ceiling and safety reserve. One instance
    per ``chunk_document`` call, so the tokenizer is loaded once.
    """

    def __init__(self, tokenizer: ModelTokenizer, cfg: ChunkingConfig) -> None:
        self.tokenizer = tokenizer
        self.cfg = cfg
        self.special_tokens = tokenizer.count("", add_special_tokens=True)
        self.payload_budget = cfg.resolved_max_tokens - cfg.safety_tokens
        # Tokens available for header + body once special tokens are set aside.
        self.available = self.payload_budget - self.special_tokens
        # The header may consume at most this, guaranteeing the body always
        # gets at least ``min_body`` tokens -- never truncate the body to
        # keep a long breadcrumb.
        self.min_body = max(1, min(cfg.min_tokens, self.available - 1))
        self.header_budget = max(1, self.available - self.min_body)

    def fit_header(self, doc_title: str, heading_path: tuple[str, ...]) -> tuple[str, bool]:
        return _fit_header(self.tokenizer, doc_title, heading_path, self.header_budget)

    def body_budget(self, header: str) -> _Budget:
        header_cost = self.tokenizer.count(header, add_special_tokens=False)
        body_max = max(1, self.available - header_cost)
        return _Budget(
            body_max=body_max,
            overlap=min(self.cfg.overlap_tokens, body_max - 1) if body_max > 1 else 0,
            min_tokens=min(self.cfg.min_tokens, body_max),
            merge_peers=self.cfg.merge_peers,
        )


def _record_payload(
    tokenizer: ModelTokenizer, contextual_text: str, diagnostics: ChunkingDiagnostics | None
) -> None:
    if diagnostics is None:
        return
    size = tokenizer.count(contextual_text, add_special_tokens=True)
    if size > diagnostics.max_payload_tokens:
        diagnostics.max_payload_tokens = size


def _table_chunk(
    unit: NormalizedUnit,
    rows: tuple[tuple[str, ...], ...],
    parent_index: int | None,
    make_contextual: _MakeContextual,
    doc_title: str,
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
        contextual_text=make_contextual(unit.heading_path, rendered, "table"),
        search_text=_search_text(doc_title, unit.heading_path, rendered),
        token_count=count_tokens(rendered),
    )


def _table_chunks(
    unit: NormalizedUnit,
    parent_index: int | None,
    budgeter: _RunBudgeter,
    make_contextual: _MakeContextual,
    diagnostics: ChunkingDiagnostics | None,
    doc_title: str,
) -> list[Chunk]:
    """One chunk for a table whose row-aware rendering already fits the
    exact contextual budget; otherwise multiple row-boundary-split chunks,
    header rows repeated at the top of each. The split threshold is this
    table's exact body budget (payload ceiling minus special tokens minus
    its fitted contextual header), so a table chunk's full embedding
    payload respects the model limit. The one remaining atomic exception
    -- a single row that alone exceeds the budget -- is handled in Phase 3.
    """
    rows = unit.table_rows or ()
    header, _ = budgeter.fit_header(doc_title, unit.heading_path)
    budget = budgeter.body_budget(header)
    whole = _table_chunk(unit, rows, parent_index, make_contextual, doc_title)
    if whole.token_count <= budget.body_max or not rows:
        return [whole]

    header_rows = rows[: unit.header_row_count]
    data_rows = rows[unit.header_row_count :]
    groups = table_renderer.split_data_rows(
        data_rows,
        header_rows=header_rows,
        max_tokens=budget.body_max,
        fixed_overhead=unit.caption or "",
    )
    if len(groups) <= 1:
        return [whole]

    if diagnostics is not None:
        diagnostics.oversized_table_rows += 1

    return [
        _table_chunk(
            unit,
            header_rows + tuple(tuple(row) for row in group),
            parent_index,
            make_contextual,
            doc_title,
        )
        for group in groups
    ]


def chunk_document(
    normalized: NormalizedDocument,
    *,
    config: ChunkingConfig | None = None,
    doc_title: str = "",
    diagnostics: ChunkingDiagnostics | None = None,
) -> list[Chunk]:
    """Chunks ``normalized`` under exact, payload-aware token budgets.

    ``doc_title`` is threaded into every chunk's ``search_text`` and
    ``contextual_text``; omit it when a caller has no title yet. Pass a
    ``ChunkingDiagnostics`` to collect split/reduction/payload counters
    (Phase 5 diagnostics); it is optional so existing callers are
    unaffected.
    """
    cfg = config or ChunkingConfig()
    tokenizer = get_model_tokenizer()
    budgeter = _RunBudgeter(tokenizer, cfg)

    old_heading_to_new: dict[int, int] = {}
    chunks: list[Chunk] = []
    pending: list[NormalizedUnit] = []

    def note_reduction(kind: str) -> None:
        if diagnostics is not None:
            diagnostics.context_headers_reduced += 1
            diagnostics.reductions_by_kind[kind] = diagnostics.reductions_by_kind.get(kind, 0) + 1

    def make_contextual(heading_path: tuple[str, ...], body: str, kind: str) -> str:
        header, reduced = budgeter.fit_header(doc_title, heading_path)
        if reduced:
            note_reduction(kind)
        contextual = _contextual_text(header, body)
        _record_payload(tokenizer, contextual, diagnostics)
        return contextual

    def finalize_group(
        group: list[_Piece], heading_path: tuple[str, ...], parent_index: int | None
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
            contextual_text=make_contextual(heading_path, text, "paragraph"),
            search_text=_search_text(doc_title, heading_path, text),
            token_count=count_tokens(text),
        )

    def flush_pending() -> None:
        nonlocal pending
        if not pending:
            return
        first = pending[0]
        parent_new = (
            old_heading_to_new.get(first.parent_index) if first.parent_index is not None else None
        )
        # Body budget is set by this run's (fitted) header.
        header, _ = budgeter.fit_header(doc_title, first.heading_path)
        budget = budgeter.body_budget(header)
        pieces: list[_Piece] = []
        for unit in pending:
            unit_pieces = _pieces_for_unit(unit, budget.body_max)
            if len(unit_pieces) > 1 and diagnostics is not None:
                diagnostics.chunks_split_by_budget += 1
            pieces.extend(unit_pieces)
        groups = _merge_peers(_pack_pieces(pieces, budget), budget)
        for group in groups:
            if group:
                chunks.append(finalize_group(group, first.heading_path, parent_new))
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
            chunks.extend(
                _table_chunks(unit, parent_new, budgeter, make_contextual, diagnostics, doc_title)
            )
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
                contextual_text=make_contextual(unit.heading_path, countable, unit.kind),
                search_text=_search_text(doc_title, unit.heading_path, countable),
                token_count=count_tokens(countable),
            )
        )
        old_heading_to_new[old_index] = new_index

    flush_pending()
    return chunks
