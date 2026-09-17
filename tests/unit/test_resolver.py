from __future__ import annotations

from ragmonk.code.resolver import resolve_reference
from ragmonk.core.models import Confidence, Entity, EntityType


def _entity(entity_id: str, name: str, qualified_name: str, *, file_id: str = "f1") -> Entity:
    return Entity(
        id=entity_id,
        source_id="s1",
        file_id=file_id,
        kind=EntityType.FUNCTION,
        name=name,
        qualified_name=qualified_name,
        language="python",
        start_line=1,
        end_line=1,
        generation=1,
        created_at="now",
        updated_at="now",
    )


def test_same_file_unambiguous_is_exact() -> None:
    bark = _entity("e1", "bark", "Dog.bark")
    result = resolve_reference(
        "self.bark",
        same_file_entities=[bark],
        qualified_name_lookup=lambda _: [],
        name_lookup=lambda _: [],
    )
    assert result.entity_id == "e1"
    assert result.confidence is Confidence.EXACT
    assert result.resolver == "same_file_name"


def test_same_file_qualified_text_match_is_exact() -> None:
    bark = _entity("e1", "bark", "Dog.bark")
    result = resolve_reference(
        "Dog.bark",
        same_file_entities=[bark],
        qualified_name_lookup=lambda _: [],
        name_lookup=lambda _: [],
    )
    assert result.entity_id == "e1"
    assert result.confidence is Confidence.EXACT
    assert result.resolver == "same_file_qualified"


def test_same_file_ambiguous_name_is_high() -> None:
    a = _entity("e1", "bark", "Dog.bark")
    b = _entity("e2", "bark", "Wolf.bark")
    result = resolve_reference(
        "bark",
        same_file_entities=[a, b],
        qualified_name_lookup=lambda _: [],
        name_lookup=lambda _: [],
    )
    assert result.entity_id in ("e1", "e2")
    assert result.confidence is Confidence.HIGH
    assert result.resolver == "same_file_name_ambiguous"


def test_cross_file_qualified_match_is_high() -> None:
    target = _entity("e9", "bark", "pkg.Dog.bark", file_id="other_file")
    result = resolve_reference(
        "pkg.Dog.bark",
        same_file_entities=[],
        qualified_name_lookup=lambda text: [target] if text == "pkg.Dog.bark" else [],
        name_lookup=lambda _: [],
    )
    assert result.entity_id == "e9"
    assert result.confidence is Confidence.HIGH
    assert result.resolver == "cross_file_qualified"


def test_cross_file_bare_name_only_is_medium() -> None:
    target = _entity("e9", "bark", "pkg.Dog.bark", file_id="other_file")
    result = resolve_reference(
        "d.bark",
        same_file_entities=[],
        qualified_name_lookup=lambda _: [],
        name_lookup=lambda name: [target] if name == "bark" else [],
    )
    assert result.entity_id == "e9"
    assert result.confidence is Confidence.MEDIUM
    assert result.resolver == "name_only"


def test_unresolved_reference_is_kept_not_dropped() -> None:
    result = resolve_reference(
        "some_external_lib.frobnicate",
        same_file_entities=[],
        qualified_name_lookup=lambda _: [],
        name_lookup=lambda _: [],
    )
    assert result.entity_id is None
    assert result.symbol == "frobnicate"
    assert result.confidence is Confidence.MEDIUM
    assert result.resolver == "unresolved"


def test_resolver_never_produces_heuristic_confidence() -> None:
    # HEURISTIC is reserved for framework_rules.py -- the generic resolver
    # must never emit it, however uncertain a match is.
    for text in ("x", "a.b.c", "totally.unknown.thing"):
        result = resolve_reference(
            text,
            same_file_entities=[],
            qualified_name_lookup=lambda _: [],
            name_lookup=lambda _: [],
        )
        assert result.confidence is not Confidence.HEURISTIC
