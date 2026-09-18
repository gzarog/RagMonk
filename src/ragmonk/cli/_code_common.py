"""Shared formatting for the Phase 2 code-graph CLI commands."""

from __future__ import annotations

from typing import Any

from rich.table import Table

from ragmonk.code.graph import DEFAULT_LIMIT, DEFAULT_MAX_DEPTH, SourceMatch, TraversalEdge
from ragmonk.core.models import Entity

from ._common import console


def entity_to_dict(
    entity: Entity, *, source_id: str | None = None, source_path: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entity_id": entity.id,
        "kind": entity.kind.value,
        "name": entity.name,
        "qualified_name": entity.qualified_name,
        "language": entity.language,
        "file_id": entity.file_id,
        "start_line": entity.start_line,
        "end_line": entity.end_line,
        "signature": entity.signature,
    }
    if source_id is not None:
        payload["source_id"] = source_id
        payload["source_path"] = source_path
    return payload


def match_to_dict(match: SourceMatch) -> dict[str, Any]:
    return entity_to_dict(match.entity, source_id=match.source_id, source_path=match.source_path)


def edge_to_dict(edge: TraversalEdge) -> dict[str, Any]:
    rel = edge.relationship
    return {
        "depth": edge.depth,
        "relationship_type": rel.relationship_type.value,
        "source_entity_id": rel.source_entity_id,
        "target_entity_id": rel.target_entity_id,
        "target_symbol": rel.target_symbol,
        "confidence": rel.confidence.value,
        "resolver": rel.resolver,
        "source_location": rel.source_location,
        "evidence": rel.evidence,
    }


def render_matches_table(matches: list[SourceMatch]) -> None:
    table = Table("Kind", "Qualified name", "Language", "Location", "Source")
    for match in matches:
        entity = match.entity
        table.add_row(
            entity.kind.value,
            entity.qualified_name,
            entity.language,
            f"{entity.file_id}:{entity.start_line}",
            match.source_id,
        )
    console.print(table)


def render_edges_table(edges: list[TraversalEdge]) -> None:
    table = Table("Depth", "Type", "Confidence", "Target", "Location")
    for edge in edges:
        rel = edge.relationship
        target = rel.target_entity_id or f"~{rel.target_symbol}"
        table.add_row(
            str(edge.depth),
            rel.relationship_type.value,
            rel.confidence.value,
            target,
            rel.source_location or "-",
        )
    console.print(table)


def no_matches_message(name: str) -> str:
    return f"No entities found matching '{name}'."


__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_DEPTH",
    "edge_to_dict",
    "entity_to_dict",
    "match_to_dict",
    "no_matches_message",
    "render_edges_table",
    "render_matches_table",
]
