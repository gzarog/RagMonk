"""Unit tests for each of ``knowledge/linker.py``'s matching methods in
isolation, with hand-constructed entity/document fixtures -- no database.
"""

from __future__ import annotations

from ragmonk.core.models import (
    Confidence,
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
    SectionKind,
)
from ragmonk.knowledge import linker
from ragmonk.storage.repositories.documents_repo import DocumentUnit


def _entity(
    entity_id: str,
    name: str,
    qualified_name: str,
    *,
    kind: EntityType = EntityType.FUNCTION,
    file_id: str = "code_f1",
) -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=kind,
        name=name,
        qualified_name=qualified_name,
        language="python",
        start_line=1,
        end_line=5,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def _unit(
    unit_id: str, text: str, *, document_id: str = "doc1", file_id: str = "doc_f1"
) -> DocumentUnit:
    return DocumentUnit(
        id=unit_id,
        document_id=document_id,
        file_id=file_id,
        kind=SectionKind.PARAGRAPH,
        text=text,
    )


def _relationship(
    *, source_entity_id: str, target_symbol: str, resolver: str = "framework:python_route_decorator"
) -> Relationship:
    return Relationship(
        id="r1",
        relationship_type=RelationshipType.REFERENCES,
        source_entity_id=source_entity_id,
        target_entity_id=None,
        target_symbol=target_symbol,
        resolver=resolver,
        confidence=Confidence.HEURISTIC,
        file_id="code_f1",
        source_location="app.py",
        evidence=target_symbol,
        generation=1,
        created_at="now",
    )


# -- exact identifier -------------------------------------------------


def test_exact_identifier_match_is_high_confidence_mentioned_in() -> None:
    dog_bark = _entity("e1", "bark", "pkg.Dog.bark")
    unit = _unit("u1", "Calling bark() logs the sound.")
    results = linker.match_exact_identifier([dog_bark], [unit])
    assert len(results) == 1
    result = results[0]
    assert result.entity_id == "e1"
    assert result.document_id == "doc1"
    assert result.section_id == "u1"
    assert result.confidence is Confidence.HIGH
    assert result.link_type is RelationshipType.MENTIONED_IN
    assert result.resolver == "linker:exact_identifier"


def test_exact_identifier_match_respects_word_boundaries() -> None:
    dog = _entity("e1", "Dog", "pkg.Dog")
    unit = _unit("u1", "See the Doghouse chapter for details.")
    assert linker.match_exact_identifier([dog], [unit]) == []


# -- qualified identifier ----------------------------------------------


def test_qualified_identifier_match_is_high_confidence_documented_by() -> None:
    dog_bark = _entity("e1", "bark", "pkg.Dog.bark")
    unit = _unit("u1", "See pkg.Dog.bark for the implementation.")
    results = linker.match_qualified_identifier([dog_bark], [unit])
    assert len(results) == 1
    result = results[0]
    assert result.confidence is Confidence.HIGH
    assert result.link_type is RelationshipType.DOCUMENTED_BY
    assert result.resolver == "linker:qualified_identifier"
    assert result.evidence == "pkg.Dog.bark"


def test_qualified_identifier_match_skipped_when_name_has_no_dot() -> None:
    bare = _entity("e1", "bark", "bark")
    unit = _unit("u1", "bark is documented here.")
    assert linker.match_qualified_identifier([bare], [unit]) == []


def test_qualified_match_is_at_least_as_strong_as_bare_match() -> None:
    from ragmonk.knowledge import confidence

    dog_bark = _entity("e1", "bark", "pkg.Dog.bark")
    unit = _unit("u1", "pkg.Dog.bark logs a bark when called.")
    bare_result = linker.match_exact_identifier([dog_bark], [unit])[0]
    qualified_result = linker.match_qualified_identifier([dog_bark], [unit])[0]
    assert confidence.at_least(qualified_result.confidence, bare_result.confidence)


# -- filename ------------------------------------------------------------


def test_filename_match_is_medium_confidence_on_namespace_entity() -> None:
    # A non-namespace entity in the same file (`other`) is deliberately
    # not passed to match_filename at all -- filename matches attach only
    # to the file's namespace entity, never fanned out to every entity a
    # file happens to define.
    namespace = _entity("ns1", "resolver", "resolver", kind=EntityType.NAMESPACE)
    unit = _unit("u1", "See resolver.py for the confidence ladder.")
    results = linker.match_filename(namespace, ["resolver.py", "resolver"], [unit])
    assert len(results) == 1
    result = results[0]
    assert result.entity_id == "ns1"
    assert result.confidence is Confidence.MEDIUM
    assert result.link_type is RelationshipType.MENTIONED_IN
    assert result.resolver == "linker:filename"


def test_filename_match_returns_nothing_without_a_namespace_entity() -> None:
    unit = _unit("u1", "See resolver.py for details.")
    assert linker.match_filename(None, ["resolver.py", "resolver"], [unit]) == []


# -- alias -----------------------------------------------------------------


def test_alias_match_is_medium_confidence() -> None:
    dog_bark = _entity("e1", "bark", "myproject.models.Dog.bark")
    unit = _unit("u1", "Dog.bark returns a string.")
    results = linker.match_alias([dog_bark], [unit])
    assert len(results) == 1
    result = results[0]
    assert result.confidence is Confidence.MEDIUM
    assert result.link_type is RelationshipType.MENTIONED_IN
    assert result.resolver == "linker:alias"
    assert result.evidence == "Dog.bark"


def test_alias_skipped_for_short_qualified_names() -> None:
    dog_bark = _entity("e1", "bark", "Dog.bark")  # only 2 segments
    unit = _unit("u1", "Dog.bark returns a string.")
    assert linker.match_alias([dog_bark], [unit]) == []


# -- route heuristic ---------------------------------------------------


def test_route_heuristic_match_is_heuristic_confidence_related_to() -> None:
    rel = _relationship(source_entity_id="e1", target_symbol="http_endpoint:GET:/dogs")
    unit = _unit("u1", "A GET request to /dogs lists all dogs.")
    results = linker.match_route_heuristic([rel], [unit])
    assert len(results) == 1
    result = results[0]
    assert result.entity_id == "e1"
    assert result.confidence is Confidence.HEURISTIC
    assert result.link_type is RelationshipType.RELATED_TO
    assert result.resolver == "linker:route_heuristic"


def test_route_heuristic_ignores_non_route_relationships() -> None:
    rel = _relationship(source_entity_id="e1", target_symbol="not_a_route")
    unit = _unit("u1", "not_a_route mentioned here")
    assert linker.match_route_heuristic([rel], [unit]) == []


def test_route_heuristic_never_produces_exact_or_high() -> None:
    rel = _relationship(source_entity_id="e1", target_symbol="http_endpoint:GET:/dogs")
    unit = _unit("u1", "GET /dogs")
    results = linker.match_route_heuristic([rel], [unit])
    assert all(r.confidence not in (Confidence.EXACT, Confidence.HIGH) for r in results)
