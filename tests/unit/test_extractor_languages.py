"""Golden tests: one hand-verified fixture per fully-supported language,
asserting entity kinds/names, at least one CALLS edge, at least one
IMPORTS edge, and (where the language has the concept) one inheritance/
interface edge.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from ragmonk.code.extractor import ExtractionResult, default_namespace_for_path, extract
from ragmonk.core.models import EntityType, RelationshipType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "languages"


def _extract(language: str, filename: str) -> ExtractionResult:
    path = FIXTURES / language.replace("csharp", "csharp") / filename
    source = path.read_bytes()
    name, qualified_name = default_namespace_for_path(PurePosixPath(filename))
    return extract(
        source,
        language,
        default_namespace_name=name,
        default_namespace_qualified_name=qualified_name,
    )


def _kinds_by_qualified_name(result: ExtractionResult) -> dict[str, EntityType]:
    return {e.qualified_name: e.kind for e in result.entities}


def test_python_golden() -> None:
    result = _extract("python", "animals.py")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["animals.Animal"] is EntityType.CLASS
    assert kinds["animals.Dog"] is EntityType.CLASS
    assert kinds["animals.Dog.bark"] is EntityType.METHOD
    assert kinds["animals.Dog.speak"] is EntityType.METHOD
    assert kinds["animals.Dog.species"] is EntityType.FIELD
    assert kinds["animals.list_dogs"] is EntityType.FUNCTION

    assert {i.module for i in result.imports} == {"os", "typing"}
    assert any(c.callee_text == "self.speak" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.EXTENDS and inh.object_name == "Animal"
        for inh in result.inherits
    )
    assert any("route" in d.text for d in result.decorators)


def test_javascript_golden() -> None:
    result = _extract("javascript", "animals.js")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["animals.Animal"] is EntityType.CLASS
    assert kinds["animals.Dog"] is EntityType.CLASS
    assert kinds["animals.Dog.bark"] is EntityType.METHOD
    assert kinds["animals.makeDog"] is EntityType.FUNCTION

    assert {i.module for i in result.imports} == {"fs", "node:fs"}
    assert any(c.callee_text == "this.speak" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.EXTENDS and inh.object_name == "Animal"
        for inh in result.inherits
    )


def test_typescript_golden() -> None:
    result = _extract("typescript", "animals.ts")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["animals.Shape"] is EntityType.INTERFACE
    assert kinds["animals.Circle"] is EntityType.CLASS
    assert kinds["animals.Color"] is EntityType.ENUM
    assert kinds["animals.Circle.area"] is EntityType.METHOD

    assert {i.module for i in result.imports} == {"./base", "./foo"}
    assert any(c.callee_text == "this.compute" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.EXTENDS and inh.object_name == "Component"
        for inh in result.inherits
    )
    assert any(
        inh.relationship_type is RelationshipType.IMPLEMENTS and inh.object_name == "Shape"
        for inh in result.inherits
    )


def test_go_golden() -> None:
    result = _extract("go", "animals.go")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["animals"] is EntityType.NAMESPACE
    assert kinds["animals.Animal"] is EntityType.INTERFACE
    assert kinds["animals.Dog"] is EntityType.STRUCT
    assert kinds["animals.Dog.Name"] is EntityType.FIELD
    assert kinds["animals.Dog.Speak"] is EntityType.METHOD
    assert kinds["animals.NewDog"] is EntityType.FUNCTION

    assert {i.module for i in result.imports} == {"fmt", "strings"}
    assert any(c.callee_text == "fmt.Sprintf" for c in result.calls)


def test_java_golden() -> None:
    result = _extract("java", "Animals.java")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["com.example.animals"] is EntityType.NAMESPACE
    assert kinds["com.example.animals.Animal"] is EntityType.INTERFACE
    assert kinds["com.example.animals.Dog"] is EntityType.CLASS
    assert kinds["com.example.animals.Dog.bark"] is EntityType.METHOD

    assert {i.module for i in result.imports} == {"java.util.List", "java.util.ArrayList"}
    assert any(c.callee_text == "bark" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.IMPLEMENTS and inh.object_name == "Animal"
        for inh in result.inherits
    )
    assert any(
        inh.relationship_type is RelationshipType.EXTENDS and inh.object_name == "Dog"
        for inh in result.inherits
    )


def test_rust_golden() -> None:
    result = _extract("rust", "animals.rs")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["animals.Animal"] is EntityType.INTERFACE
    assert kinds["animals.Dog"] is EntityType.STRUCT
    assert kinds["animals.Dog.bark"] is EntityType.METHOD
    assert kinds["animals.make_dog"] is EntityType.FUNCTION

    assert {i.module for i in result.imports} == {"std::fmt", "std::collections::HashMap"}
    assert any(c.callee_text == "self.bark" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.IMPLEMENTS
        and inh.object_name == "Animal"
        and inh.subject_name == "Dog"
        for inh in result.inherits
    )


def test_csharp_golden() -> None:
    result = _extract("csharp", "Animals.cs")
    kinds = _kinds_by_qualified_name(result)

    assert kinds["Animals"] is EntityType.NAMESPACE
    assert kinds["Animals.IAnimal"] is EntityType.INTERFACE
    assert kinds["Animals.Dog"] is EntityType.CLASS
    assert kinds["Animals.Dog.Name"] is EntityType.PROPERTY
    assert kinds["Animals.Dog.Speak"] is EntityType.METHOD

    assert {i.module for i in result.imports} == {"System", "System.Collections.Generic"}
    assert any(c.callee_text == "Bark" for c in result.calls)
    assert any(
        inh.relationship_type is RelationshipType.EXTENDS and inh.object_name == "IAnimal"
        for inh in result.inherits
    )
    assert any("Route" in d.text for d in result.decorators)


@pytest.mark.parametrize(
    ("language", "filename"),
    [
        ("python", "animals.py"),
        ("javascript", "animals.js"),
        ("typescript", "animals.ts"),
        ("go", "animals.go"),
        ("java", "Animals.java"),
        ("rust", "animals.rs"),
        ("csharp", "Animals.cs"),
    ],
)
def test_every_fixture_parses_without_error(language: str, filename: str) -> None:
    from ragmonk.code.parser import parse

    source = (FIXTURES / language / filename).read_bytes()
    tree = parse(source, language)
    assert not tree.root_node.has_error
