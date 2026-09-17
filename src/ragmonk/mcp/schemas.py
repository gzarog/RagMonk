"""Pydantic input/output models for every ``ragmonk_*`` MCP tool.

One explicit output model per tool (not "whatever dict falls out") so a
client sees a stable, versioned shape -- ``BaseToolOutput.schema_version``
mirrors the CLI's own ``{"schema_version": "1", "data": {...}}`` envelope
convention (``cli/_common.py``), and ``ok``/``error`` give every tool the
same success/failure shape instead of raising a raw exception across the
MCP boundary (see ``mcp/tools.py``'s ``_execute``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class ToolError(BaseModel):
    """A clear, typed error -- never a raw traceback. ``type`` is the
    RagMonk exception class name (``"UsageError"``, ``"ConfigError"``,
    ...) or one of this module's own boundary types (``"timeout"``,
    ``"internal_error"``), so a client can branch on it without parsing
    prose.
    """

    type: str
    message: str


class BaseToolOutput(BaseModel):
    schema_version: str = SCHEMA_VERSION
    ok: bool
    error: ToolError | None = None


# -- shared row shapes, reused across tools -----------------------------


class EntityMatch(BaseModel):
    """One resolved code entity -- the same shape ``cli/_code_common.py``'s
    ``match_to_dict`` produces for ``ragmonk symbol --json`` etc.
    """

    entity_id: str
    kind: str
    name: str
    qualified_name: str
    language: str
    file_id: str
    start_line: int
    end_line: int
    signature: str | None = None
    source_id: str | None = None
    source_path: str | None = None


class GraphEdge(BaseModel):
    """One traversal hop -- the same shape ``edge_to_dict`` produces for
    ``ragmonk callers/callees --json``.
    """

    depth: int
    relationship_type: str
    source_entity_id: str
    target_entity_id: str | None = None
    target_symbol: str | None = None
    confidence: str
    resolver: str
    source_location: str | None = None
    evidence: str | None = None


class LexicalHit(BaseModel):
    """One ranked lexical result -- ``retrieval/lexical.py``'s
    ``SearchResult.to_dict()`` shape, shared by ``ragmonk_search`` and
    ``ragmonk_explore``'s ``documents`` list.
    """

    kind: str
    tier: str
    id: str
    title: str
    path: str
    source_id: str
    snippet: str | None = None
    location: dict[str, Any] | None = None


class EvidenceFact(BaseModel):
    """``knowledge/evidence.py``'s ``Evidence.to_dict()`` shape: a bare
    relationship-shaped fact with no source snippet attached -- what
    ``ragmonk_impact``'s ``documentation`` list carries.
    """

    source: str
    path: str
    location: dict[str, Any]
    entity: str
    relationship: str
    confidence: str


class EvidenceRef(EvidenceFact):
    """An ``EvidenceFact`` plus the source snippet that lets a reader
    interpret it -- ``retrieval/context_builder.py``'s budgeted
    ``EvidenceItem.to_dict()`` shape, used by ``ragmonk_explore``.
    """

    snippet: str


# -- ragmonk_explore -----------------------------------------------------


class ExploreInput(BaseModel):
    query: str
    max_chars: int | None = Field(default=None, description="Override context.max_chars.")
    max_files: int | None = Field(default=None, description="Override context.max_files.")
    max_graph_nodes: int | None = Field(
        default=None, description="Override context.max_graph_nodes."
    )


class RelationshipEdge(BaseModel):
    source: str
    relationship: str
    target: str
    confidence: str


class ExploreEntity(BaseModel):
    """A resolved symbol as ``explore``'s query planner sees it -- fewer
    fields than a full ``EntityMatch`` (no file/line/signature detail;
    that lives in ``evidence`` instead).
    """

    entity_id: str
    qualified_name: str
    kind: str
    source_id: str


class ExploreOutput(BaseToolOutput):
    query: str | None = None
    intent: str | None = None
    strategies: list[str] = Field(default_factory=list)
    answer_context: str | None = None
    entities: list[ExploreEntity] = Field(default_factory=list)
    relationships: list[RelationshipEdge] = Field(default_factory=list)
    documents: list[LexicalHit] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# -- ragmonk_search --------------------------------------------------------


class SearchInput(BaseModel):
    query: str
    limit: int | None = None


class SearchOutput(BaseToolOutput):
    query: str | None = None
    results: list[LexicalHit] = Field(default_factory=list)


# -- ragmonk_symbol / ragmonk_callers / ragmonk_callees -----------------


class SymbolInput(BaseModel):
    name: str


class SymbolOutput(BaseToolOutput):
    query: str | None = None
    matches: list[EntityMatch] = Field(default_factory=list)


class GraphTraversalInput(BaseModel):
    name: str
    max_depth: int | None = None
    limit: int | None = None


class GraphTraversalOutput(BaseToolOutput):
    query: str | None = None
    matches: list[EntityMatch] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


# -- ragmonk_impact ---------------------------------------------------------


class ImpactInput(BaseModel):
    name: str
    max_depth: int | None = None
    limit: int | None = None


class DefinedLocation(BaseModel):
    entity_id: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    source_id: str


class ConfidenceSummary(BaseModel):
    code_references: str | None = None
    document_links: str | None = None


class ImpactOutput(BaseToolOutput):
    query: str | None = None
    found: bool = False
    defined: list[DefinedLocation] = Field(default_factory=list)
    callers: list[str] = Field(default_factory=list)
    callees: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    documentation: list[EvidenceFact] = Field(default_factory=list)
    confidence: ConfidenceSummary | None = None
    blast_radius: str | None = None
    blast_radius_score: int | None = None


# -- ragmonk_documents ------------------------------------------------------


class DocumentsInput(BaseModel):
    source_id: str | None = None


class DocumentRow(BaseModel):
    source_id: str
    file_id: str
    path: str
    status: str
    format: str | None = None
    title: str | None = None
    page_count: int | None = None
    section_count: int
    paragraph_count: int
    table_count: int
    is_scanned: bool


class DocumentsOutput(BaseToolOutput):
    documents: list[DocumentRow] = Field(default_factory=list)


# -- ragmonk_status -----------------------------------------------------


class StatusInput(BaseModel):
    pass


class SourceStatus(BaseModel):
    id: str
    path: str
    enabled: bool
    # "active" | "offline" (``core.models.SourceStatus``, Phase 7) -- kept
    # as a plain string here rather than importing that enum, matching
    # this schema module's existing convention of not depending on
    # core.models.
    status: str = "active"
    counts: dict[str, int]
    queue_depth: int
    last_scan_at: str | None = None
    last_error: str | None = None


class TotalsSummary(BaseModel):
    by_status: dict[str, int]
    queue_depth: int


class StatusOutput(BaseToolOutput):
    sources: list[SourceStatus] = Field(default_factory=list)
    totals: TotalsSummary | None = None


# -- ragmonk_ask -------------------------------------------------------


class AskInput(BaseModel):
    question: str


class AiUsageOut(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class AiAnswerOut(BaseModel):
    text: str
    provider: str
    model: str
    usage: AiUsageOut


class AskOutput(BaseToolOutput):
    """Not read-only and not network-free like the other 8 tools (see
    ``mcp/server.py``'s per-tool annotations/instructions) -- calling this
    tool reaches the configured ``ai:`` provider, which may be a real
    cloud/network endpoint gated by ``privacy.external_ai_allowed``
    exactly like ``ragmonk ask`` itself.
    """

    question: str | None = None
    answer: AiAnswerOut | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
