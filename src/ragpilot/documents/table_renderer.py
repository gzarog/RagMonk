"""Row-aware textual rendering of a table's grid, for search/embedding
indexing and for splitting an oversized table by row boundaries.

Search Quality Improvement Plan, Phase 4: before this module existed, a
table's only textual representation was ``" ".join(cell for row in
table_rows for cell in row)`` (see git history for
``documents_repo.insert_table``'s previous ``flattened`` local) -- every
cell's row association was lost the moment it was flattened, so a query
like "which server has 85% CPU?" could not be answered even when both
``api-02`` and ``85%`` were indexed: nothing tied them to the same row,
and a large enough table meant an unrelated ``85%`` from a completely
different row could satisfy the query just as well.

``render_rows`` instead renders one line per row, cells pipe-delimited in
column order:

    Server | CPU | RAM | Status
    api-01 | 40% | 8GB | healthy
    api-02 | 85% | 16GB | degraded

A plain-text line is still just tokens to FTS5/an embedding model, but
now the *tokens on one line* are exactly the tokens that were in the same
row -- BM25's proximity-agnostic bag-of-words scoring already does much
better at ranking a document up when a query's terms co-occur on one
short line rather than being scattered across a much longer blob, and a
sentence-embedding model sees "api-02 | 85% | 16GB | degraded" as one
coherent unit rather than losing that association entirely.

``split_data_rows`` is the row-boundary counterpart to
``tokenization.split_by_token_budget``: a table whose full rendering
exceeds a chunk's ``max_tokens`` budget is split at row boundaries only
(never mid-row -- a half-row is meaningless), with the header rows
repeated verbatim at the top of every resulting group so each one stays
independently interpretable (see ``documents/chunker.py``'s Phase 4
wiring).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from ragpilot.documents.tokenization import count_tokens as _count_tokens

# Cells are pipe-delimited rather than space-joined (the previous
# behavior) specifically so a cell that itself contains spaces ("New
# York") doesn't visually blur into its neighbor -- '|' is also how
# Markdown itself denotes a table row, so this rendering reads naturally
# to anyone who has seen a Markdown table, without pulling in a full
# Markdown table renderer (alignment rows, escaping, etc.) this project
# has no use for.
_CELL_SEPARATOR = " | "


def render_rows(rows: Sequence[Sequence[str]]) -> str:
    """Row-preserving textual rendering of a table's grid: one line per
    row, in row order, cells pipe-delimited in column order. The header
    row (if any) is not distinguished by any special marker -- it is
    simply row 0, exactly as a reader scanning the rendered text would
    expect a table's own header to be its first line.
    """
    return "\n".join(_CELL_SEPARATOR.join(cell for cell in row) for row in rows)


def render_table(rows: Sequence[Sequence[str]], *, caption: str | None = None) -> str:
    """The full text a table (or one row-boundary split of one) should
    contribute to search/embedding indexing: its caption, when present,
    as a leading line, then the row-preserving grid (``render_rows``).

    Heading-path/document-title context is deliberately *not* composed in
    here -- that is a chunk-kind-agnostic decision (every chunk kind
    needs it, not just tables) that belongs to whatever generic
    title+heading-path+content assembly scheme a later phase establishes
    for ``search_text``/``embedding_text`` (see ``chunker.py``'s
    ``_contextual_text``, and ``documents_repo.py``'s callers of this
    function); this table-specific renderer only owns the "content" half
    of that composition -- caption and row-aware cell text -- so it can
    be composed into that scheme unchanged once it exists.
    """
    body = render_rows(rows)
    if caption and body:
        return f"{caption}\n\n{body}"
    return caption or body


def split_data_rows(
    data_rows: Sequence[Sequence[str]],
    *,
    header_rows: Sequence[Sequence[str]],
    max_tokens: int,
    fixed_overhead: str = "",
    count_tokens: Callable[[str], int] = _count_tokens,
) -> list[list[Sequence[str]]]:
    """Greedily groups ``data_rows`` (a table's rows *after* its header
    block) into row-boundary chunks, each sized so that rendering it back
    together with ``header_rows`` repeated at the top and
    ``fixed_overhead`` (e.g. the table's caption -- present in every
    resulting group's own rendering, so it must count against every
    group's budget too) fits within ``max_tokens``.

    Never a mid-row split: a single data row that alone -- combined with
    the repeated header and fixed overhead -- still exceeds
    ``max_tokens`` is kept whole in its own one-row group rather than
    further split, since there is no meaningful sub-row unit to cut at.
    This is the same "atomic when a real unit can't be shrunk any
    further" exception ``chunk_document`` documented for whole tables
    before this phase, now correctly scoped to one oversized row instead
    of forcing the entire table to stay unsplit.

    Returns ``[]`` for empty ``data_rows`` (nothing to split) rather than
    ``[[]]`` -- callers should treat that as "no split needed", not as
    one empty group.
    """
    if not data_rows:
        return []

    header_text = render_rows(header_rows) if header_rows else ""

    def _rendered_tokens(group: Sequence[Sequence[str]]) -> int:
        parts = [part for part in (fixed_overhead, header_text, render_rows(group)) if part]
        return count_tokens("\n\n".join(parts))

    groups: list[list[Sequence[str]]] = []
    current: list[Sequence[str]] = []
    for row in data_rows:
        candidate = [*current, row]
        if current and _rendered_tokens(candidate) > max_tokens:
            groups.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups
