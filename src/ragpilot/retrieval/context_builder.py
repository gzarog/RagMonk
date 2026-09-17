"""Assembles a budgeted, deduplicated evidence package (blueprint section
27) for a retrieval caller -- ``explore`` now, Phase 6's MCP tools later.

A pure function over already-fetched ``Evidence``/``GraphPath`` values,
with no database access of its own: every caller already has to look up
entities/relationships/documents to build an ``Evidence`` object in the
first place (``knowledge/evidence.py``), so this module's only job is
dedup + priority + budget, not another round of storage reads. That also
makes it trivially unit-testable without a database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from ragpilot.core.config import ContextConfig, SearchContextConfig
from ragpilot.documents.tokenization import count_tokens
from ragpilot.knowledge import confidence as confidence_rank
from ragpilot.knowledge.evidence import Evidence
from ragpilot.storage.repositories import documents_repo
from ragpilot.storage.repositories.documents_repo import DocumentUnit

_LocationKey = tuple[str, Any, Any, Any, Any]


@dataclass(frozen=True)
class EvidenceItem:
    """An ``Evidence`` fact plus the source snippet that lets a reader
    actually interpret it (blueprint step 7: "preserve enough source for
    interpretation") -- ``Evidence`` itself carries only location/
    provenance, never text, so the snippet travels alongside it rather
    than inside it.
    """

    evidence: Evidence
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = self.evidence.to_dict()
        payload["snippet"] = self.snippet
        return payload


@dataclass(frozen=True)
class GraphPath:
    """One relationship hop rendered as ``A -[REL]-> B``, not just the
    two bare endpoints -- blueprint step 5, "include graph paths (how A
    relates to B), not just the endpoints".
    """

    source: str
    relationship: str
    target: str
    confidence: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "relationship": self.relationship,
            "target": self.target,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ContextResult:
    evidence: list[dict[str, Any]]
    graph_paths: list[dict[str, str]]
    truncated: bool
    truncation_reasons: list[str]
    total_chars: int
    file_count: int
    graph_node_count: int


def _location_key(item: EvidenceItem) -> _LocationKey:
    loc = item.evidence.location
    return (item.evidence.path, loc.line_start, loc.line_end, loc.page, loc.section)


def _dedupe(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Blueprint step 1: the same file/location shouldn't appear twice.
    When two items share a location, the higher-confidence one wins;
    ties keep whichever came first, so the result is deterministic for a
    given, already-deterministic input order.
    """
    best: dict[_LocationKey, EvidenceItem] = {}
    order: list[_LocationKey] = []
    for item in items:
        key = _location_key(item)
        current = best.get(key)
        if current is None:
            order.append(key)
            best[key] = item
        elif confidence_rank.rank(item.evidence.confidence) > confidence_rank.rank(
            current.evidence.confidence
        ):
            best[key] = item
    return [best[key] for key in order]


def _none_last(value: Any) -> tuple[int, Any]:
    return (1, 0) if value is None else (0, value)


def _priority_key(item: EvidenceItem) -> tuple[Any, ...]:
    """Blueprint step 3: exact evidence before heuristic. Sorted by
    confidence rank descending, then deterministically by location/entity
    so equally-confident items always truncate in the same order across
    runs.
    """
    loc = item.evidence.location
    return (
        -confidence_rank.rank(item.evidence.confidence),
        item.evidence.path,
        _none_last(loc.line_start),
        _none_last(loc.page),
        item.evidence.entity,
    )


def build_context(
    items: list[EvidenceItem],
    graph_paths: list[GraphPath],
    *,
    budget: ContextConfig,
) -> ContextResult:
    """Dedupes, priority-sorts, then caps ``items`` to ``budget.max_files``
    distinct files and ``budget.max_chars`` total snippet characters, and
    caps ``graph_paths`` to ``budget.max_graph_nodes`` distinct endpoints
    -- truncation is explicit (``ContextResult.truncated``/
    ``truncation_reasons``), never a silent drop.

    Only snippet text counts toward ``max_chars``: it is the actual bulk
    of an evidence item's payload and the one thing that varies widely in
    size, whereas the small fixed fields (path/entity/confidence) are
    kept regardless so a truncated-to-zero-snippet item is still
    traceable to its source.
    """
    ordered = sorted(_dedupe(items), key=_priority_key)

    included: list[EvidenceItem] = []
    included_files: set[str] = set()
    total_chars = 0
    files_exceeded = False
    chars_exceeded = False

    for item in ordered:
        is_new_file = item.evidence.path not in included_files
        if is_new_file and len(included_files) >= budget.max_files:
            files_exceeded = True
            continue
        item_chars = len(item.snippet)
        if total_chars + item_chars > budget.max_chars:
            chars_exceeded = True
            continue
        included.append(item)
        included_files.add(item.evidence.path)
        total_chars += item_chars

    included_graph: list[GraphPath] = []
    nodes: set[str] = set()
    graph_exceeded = False
    for path in graph_paths:
        candidate_nodes = nodes | {path.source, path.target}
        if len(candidate_nodes) > budget.max_graph_nodes:
            graph_exceeded = True
            continue
        nodes = candidate_nodes
        included_graph.append(path)

    reasons: list[str] = []
    if files_exceeded:
        reasons.append(f"evidence truncated: max_files={budget.max_files} reached")
    if chars_exceeded:
        reasons.append(f"evidence truncated: max_chars={budget.max_chars} reached")
    if graph_exceeded:
        reasons.append(f"graph paths truncated: max_graph_nodes={budget.max_graph_nodes} reached")

    return ContextResult(
        evidence=[item.to_dict() for item in included],
        graph_paths=[path.to_dict() for path in included_graph],
        truncated=bool(reasons),
        truncation_reasons=reasons,
        total_chars=total_chars,
        file_count=len(included_files),
        graph_node_count=len(nodes),
    )


@dataclass(frozen=True)
class ChunkContextPiece:
    """One chunk rendered for display -- either the matched hit itself or
    a piece of its surrounding context (Search Quality Improvement Plan,
    Phase 9). Carrying ``kind`` ("heading"/"paragraph"/"table") lets a
    consumer distinguish a heading from body text without re-deriving it
    from ``heading_path``.
    """

    id: str
    kind: str
    text: str
    heading_path: list[str] = field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "heading_path": self.heading_path,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


def _to_piece(unit: DocumentUnit) -> ChunkContextPiece:
    return ChunkContextPiece(
        id=unit.id,
        kind=unit.kind.value,
        text=unit.text,
        heading_path=unit.heading_path,
        page_start=unit.page_start,
        page_end=unit.page_end,
    )


@dataclass(frozen=True)
class ExpandedChunkContext:
    """A matched chunk plus its expanded context, kept as two structurally
    distinct fields (blueprint Phase 9's "clearly distinguish the matched
    chunk from surrounding context") rather than one flattened list a
    consumer would have to inspect to tell apart -- ``matched`` is always
    exactly the hit that was ranked; everything else is presentation
    dressing added after ranking finished, never a ranking input.
    """

    matched: ChunkContextPiece
    parent_heading: ChunkContextPiece | None
    previous: list[ChunkContextPiece]
    next: list[ChunkContextPiece]
    truncated: bool
    truncation_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched.to_dict(),
            "parent_heading": self.parent_heading.to_dict() if self.parent_heading else None,
            "previous": [piece.to_dict() for piece in self.previous],
            "next": [piece.to_dict() for piece in self.next],
            "truncated": self.truncated,
            "truncation_reasons": self.truncation_reasons,
        }


def expand_chunk_context(
    conn: sqlite3.Connection,
    unit_id: str,
    *,
    config: SearchContextConfig,
) -> ExpandedChunkContext | None:
    """Attaches a matched chunk's nearest parent heading and previous/next
    sibling chunks (Search Quality Improvement Plan, Phase 9), strictly as
    a post-ranking presentation step: this is only ever called with a
    chunk a ranker has *already* selected, on results already sorted and
    sliced to ``--limit`` -- it has no way to change what was selected or
    its position, only what gets shown alongside it.

    Returns ``None`` when ``unit_id`` doesn't resolve to a stored chunk
    (an entity/path hit, or a document-title hit with no pinned section --
    see ``lexical.search_title_projection``) rather than raising, since a
    caller iterating mixed search-result kinds shouldn't have to
    pre-filter down to "document hits with a real section id" itself.

    The matched chunk's own text always counts toward ``config.max_tokens``
    but is never dropped for it -- it is the one piece of evidence
    provenance can never lose (blueprint requirement). Budget is then
    spent, in priority order, on the parent heading and then previous/next
    siblings nearest-first per side; the first piece that would overflow
    the budget is dropped along with everything farther from the match on
    that same side, so what is shown is always a contiguous window around
    the hit, never a random subset.
    """
    matched_unit = documents_repo.get_unit(conn, unit_id)
    if matched_unit is None:
        return None

    matched_piece = _to_piece(matched_unit)
    budget = config.max_tokens
    used = count_tokens(matched_piece.text)
    reasons: list[str] = []

    neighbors = documents_repo.get_chunk_neighbors(
        conn,
        unit_id,
        previous_chunks=config.previous_chunks,
        next_chunks=config.next_chunks,
        include_parent_heading=config.parent_heading,
    )

    parent_piece: ChunkContextPiece | None = None
    if neighbors.parent_heading is not None:
        candidate = _to_piece(neighbors.parent_heading)
        cost = count_tokens(candidate.text)
        if used + cost <= budget:
            parent_piece = candidate
            used += cost
        else:
            reasons.append(f"parent heading dropped: max_tokens={budget} reached")

    def _take_within_budget(nearest_first: list[DocumentUnit]) -> list[DocumentUnit]:
        # Walks nearest-to-farthest so the first overflow drops that
        # sibling *and* every farther one after it (never tried) --
        # what's kept is always a contiguous window against the match,
        # not an arbitrary subset.
        nonlocal used
        taken: list[DocumentUnit] = []
        for unit in nearest_first:
            cost = count_tokens(unit.text)
            if used + cost > budget:
                reasons.append(f"sibling chunk dropped: max_tokens={budget} reached")
                break
            taken.append(unit)
            used += cost
        return taken

    # ``neighbors.previous``/``.next`` are already chronological
    # (oldest-to-newest); the budget walk needs nearest-first, so
    # ``previous`` is reversed going in and reversed back on the way out.
    previous_nearest_first = list(reversed(neighbors.previous))
    previous_pieces = [
        _to_piece(unit) for unit in reversed(_take_within_budget(previous_nearest_first))
    ]
    next_pieces = [_to_piece(unit) for unit in _take_within_budget(neighbors.next)]

    return ExpandedChunkContext(
        matched=matched_piece,
        parent_heading=parent_piece,
        previous=previous_pieces,
        next=next_pieces,
        truncated=bool(reasons),
        truncation_reasons=reasons,
    )
