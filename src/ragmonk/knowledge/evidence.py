"""Normalizes any relationship-shaped fact -- a Phase 2 code relationship
or a Phase 4 cross-domain link -- into the blueprint's evidence contract
(section 57): a flat, source-labelled, location-carrying shape that Phase
5's context builder and Phase 6's MCP tools can consume without caring
whether the underlying fact came from the code graph or the document
store.

A thin mapper, not a new store: Phase 2/3 already keep enough location/
provenance on ``Entity``/``Relationship``/``DocumentUnit`` rows to
populate this at read time from ``entities_repo``/``relationships_repo``/
``documents_repo`` directly, so nothing here is persisted. See
``core.models.CrossLink``'s docstring for why cross-domain *links*
themselves still get one small dedicated table (``cross_links``) --
that's a storage-shape problem (a document is not an ``entities`` row),
unrelated to this module's job of shaping already-stored facts for
display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ragmonk.core.models import Confidence, CrossLink, Entity, Relationship
from ragmonk.storage.repositories.documents_repo import DocumentUnit


@dataclass(frozen=True)
class EvidenceLocation:
    line_start: int | None = None
    line_end: int | None = None
    page: int | None = None
    section: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_start": self.line_start,
            "line_end": self.line_end,
            "page": self.page,
            "section": self.section,
        }


@dataclass(frozen=True)
class Evidence:
    source: str
    path: str
    location: EvidenceLocation
    entity: str
    relationship: str
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "location": self.location.to_dict(),
            "entity": self.entity,
            "relationship": self.relationship,
            "confidence": self.confidence.value,
        }


def from_code_relationship(
    relationship: Relationship, *, entity: Entity, file_path: str
) -> Evidence:
    """A code-sourced fact: location is a line range, never a page/section
    (code has no notion of either). ``entity`` is the *evidenced* entity
    -- typically the relationship's source, but callers may pass whichever
    side of the edge they are presenting evidence for.
    """
    return Evidence(
        source=relationship.resolver,
        path=file_path,
        location=EvidenceLocation(line_start=entity.start_line, line_end=entity.end_line),
        entity=entity.qualified_name,
        relationship=relationship.relationship_type.value,
        confidence=relationship.confidence,
    )


def from_cross_link(
    link: CrossLink, *, entity: Entity, document_path: str, unit: DocumentUnit | None
) -> Evidence:
    """A cross-domain (code <-> document) fact: location is a page and/or
    heading-path section, never a line range -- a normalized document has
    no source line numbers (see ``documents/normalizer.py``). ``unit`` is
    the specific section/paragraph/table the link is pinned to, when one
    was recorded (``CrossLink.section_id``); ``None`` when the link only
    names the document as a whole, in which case the location carries no
    page/section either -- there is nothing narrower to point at.
    """
    return Evidence(
        source=link.resolver,
        path=document_path,
        location=EvidenceLocation(
            page=unit.page_start if unit is not None else None,
            section=(
                " > ".join(unit.heading_path) if unit is not None and unit.heading_path else None
            ),
        ),
        entity=entity.qualified_name,
        relationship=link.link_type.value,
        confidence=link.confidence,
    )
