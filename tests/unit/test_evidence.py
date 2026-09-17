"""Proves ``knowledge/evidence.py`` produces the blueprint's evidence
contract shape (section 57) for both a code-sourced and a document-
sourced (cross-domain) relationship.
"""

from __future__ import annotations

from ragmonk.core.models import (
    Confidence,
    CrossLink,
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
    SectionKind,
)
from ragmonk.knowledge import evidence
from ragmonk.storage.repositories.documents_repo import DocumentUnit

_EVIDENCE_KEYS = {"source", "path", "location", "entity", "relationship", "confidence"}
_LOCATION_KEYS = {"line_start", "line_end", "page", "section"}


def _entity() -> Entity:
    return Entity(
        id="e1",
        source_id="s1",
        file_id="f1",
        kind=EntityType.FUNCTION,
        name="bark",
        qualified_name="pkg.Dog.bark",
        language="python",
        start_line=10,
        end_line=21,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def test_code_relationship_evidence_matches_blueprint_shape() -> None:
    relationship = Relationship(
        id="r1",
        relationship_type=RelationshipType.CALLS,
        source_entity_id="e1",
        target_entity_id="e2",
        resolver="same_file_qualified",
        confidence=Confidence.EXACT,
        file_id="f1",
        source_location="src/dog.py:10",
        generation=1,
        created_at="now",
    )
    ev = evidence.from_code_relationship(relationship, entity=_entity(), file_path="src/dog.py")
    payload = ev.to_dict()

    assert set(payload.keys()) == _EVIDENCE_KEYS
    assert set(payload["location"].keys()) == _LOCATION_KEYS
    assert payload["source"] == "same_file_qualified"
    assert payload["path"] == "src/dog.py"
    assert payload["location"]["line_start"] == 10
    assert payload["location"]["line_end"] == 21
    assert payload["location"]["page"] is None
    assert payload["location"]["section"] is None
    assert payload["entity"] == "pkg.Dog.bark"
    assert payload["relationship"] == "calls"
    assert payload["confidence"] == "exact"


def test_cross_link_evidence_matches_blueprint_shape() -> None:
    link = CrossLink(
        id="cl1",
        link_type=RelationshipType.DOCUMENTED_BY,
        entity_id="e1",
        document_id="doc1",
        section_id="u1",
        resolver="linker:qualified_identifier",
        confidence=Confidence.HIGH,
        evidence="pkg.Dog.bark",
        created_at="now",
    )
    unit = DocumentUnit(
        id="u1",
        document_id="doc1",
        file_id="docf1",
        kind=SectionKind.PARAGRAPH,
        text="pkg.Dog.bark is documented here.",
        heading_path=["API Reference", "Dog"],
        page_start=3,
        page_end=3,
    )
    ev = evidence.from_cross_link(
        link, entity=_entity(), document_path="docs/reference.pdf", unit=unit
    )
    payload = ev.to_dict()

    assert set(payload.keys()) == _EVIDENCE_KEYS
    assert set(payload["location"].keys()) == _LOCATION_KEYS
    assert payload["source"] == "linker:qualified_identifier"
    assert payload["path"] == "docs/reference.pdf"
    assert payload["location"]["line_start"] is None
    assert payload["location"]["line_end"] is None
    assert payload["location"]["page"] == 3
    assert payload["location"]["section"] == "API Reference > Dog"
    assert payload["entity"] == "pkg.Dog.bark"
    assert payload["relationship"] == "documented_by"
    assert payload["confidence"] == "high"


def test_cross_link_evidence_without_a_unit_has_no_page_or_section() -> None:
    link = CrossLink(
        id="cl2",
        link_type=RelationshipType.DOCUMENTED_BY,
        entity_id="e1",
        document_id="doc1",
        section_id=None,
        resolver="user",
        confidence=Confidence.EXACT,
        evidence=None,
        created_at="now",
    )
    ev = evidence.from_cross_link(
        link, entity=_entity(), document_path="docs/reference.pdf", unit=None
    )
    payload = ev.to_dict()
    assert payload["location"]["page"] is None
    assert payload["location"]["section"] is None
    assert payload["confidence"] == "exact"
