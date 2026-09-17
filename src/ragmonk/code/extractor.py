"""Generic, query-driven fact extraction from a parsed source file.

The only language-specific knowledge lives in ``code/queries/<lang>.scm``.
This module interprets a fixed capture-name contract those files follow
(documented at the top of ``queries/python.scm``) with the same Python
code path regardless of which language produced the captures:

* ``<kind>.definition`` / ``<kind>.name`` -- an entity, ``kind`` one of
  ``namespace/class/interface/struct/enum/function/property/field``.
* ``<kind>.parent_name`` -- an explicit, name-based parent for an entity
  that is not lexically nested inside its logical container (Go methods
  via a receiver type, Rust ``impl`` block methods).
* ``extends.object`` / ``implements.object``, each optionally paired with
  a `.subject`` capture in the same match for the same reason.
* ``call.expression`` / ``call.callee``.
* ``import.module`` (optionally ``import.alias``).
* ``decorator.raw`` paired with ``decorator.subject`` bound (by shared
  node identity, not necessarily nesting) to the annotated definition.

Containment (who is CONTAINS'd by whom, and therefore each entity's
qualified name) is derived structurally by walking ``Node.parent`` to the
nearest already-known entity node -- generic across every grammar --
except where a query supplies ``parent_name`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

import tree_sitter as ts

from ragmonk.code.parser import parse
from ragmonk.code.queries import load_query
from ragmonk.core.models import EntityType, RelationshipType

_KIND_BY_PREFIX: dict[str, EntityType] = {
    "namespace": EntityType.NAMESPACE,
    "class": EntityType.CLASS,
    "interface": EntityType.INTERFACE,
    "struct": EntityType.STRUCT,
    "enum": EntityType.ENUM,
    "function": EntityType.FUNCTION,
    "property": EntityType.PROPERTY,
    "field": EntityType.FIELD,
}
# Processing tiers: structural/named parents must exist before their
# children are resolved, and namespace (the root fallback parent for
# everything) must exist before either. This ordering -- not language
# knowledge -- is what lets Go/Rust's name-based parent lookup work.
_TIER_ORDER: tuple[frozenset[str], ...] = (
    frozenset({"namespace"}),
    frozenset({"class", "interface", "struct", "enum"}),
    frozenset({"function", "property", "field"}),
)
_TYPE_LIKE = frozenset({EntityType.CLASS, EntityType.INTERFACE, EntityType.STRUCT, EntityType.ENUM})
_MAX_SIGNATURE_LEN = 200


@dataclass(frozen=True)
class ExtractedEntity:
    local_id: int
    kind: EntityType
    name: str
    qualified_name: str
    parent_local_id: int | None
    signature: str | None
    start_line: int
    end_line: int
    start_col: int
    end_col: int


@dataclass(frozen=True)
class ExtractedImport:
    module: str
    line: int


@dataclass(frozen=True)
class ExtractedCall:
    caller_local_id: int | None
    callee_text: str
    line: int


@dataclass(frozen=True)
class ExtractedInherit:
    relationship_type: RelationshipType
    subject_local_id: int | None
    subject_name: str | None
    object_name: str
    line: int


@dataclass(frozen=True)
class ExtractedDecorator:
    subject_local_id: int
    text: str
    line: int


@dataclass
class ExtractionResult:
    language: str
    entities: list[ExtractedEntity] = field(default_factory=list)
    imports: list[ExtractedImport] = field(default_factory=list)
    calls: list[ExtractedCall] = field(default_factory=list)
    inherits: list[ExtractedInherit] = field(default_factory=list)
    decorators: list[ExtractedDecorator] = field(default_factory=list)


def default_namespace_for_path(relative_path: PurePosixPath) -> tuple[str, str]:
    """Dotted module path derived from a file's project-relative path.

    Used whenever a language's query has no explicit namespace/package
    declaration to capture (Python, JS/TS, Rust's file-based modules).
    """
    parts = list(relative_path.with_suffix("").parts)
    if not parts:
        return "root", "root"
    return parts[-1], ".".join(parts)


def _strip_quotes(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _decode(source: bytes, node: ts.Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _signature(source: bytes, node: ts.Node) -> str:
    text = _decode(source, node)
    first_line = text.splitlines()[0] if text else ""
    return first_line[:_MAX_SIGNATURE_LEN]


def _nearest_ancestor_entity(node: ts.Node, node_id_to_local: dict[int, int]) -> int | None:
    current = node.parent
    while current is not None:
        local_id = node_id_to_local.get(current.id)
        if local_id is not None:
            return local_id
        current = current.parent
    return None


def extract(
    source: bytes,
    language: str,
    *,
    default_namespace_name: str,
    default_namespace_qualified_name: str,
) -> ExtractionResult:
    tree = parse(source, language)
    query = load_query(language)
    cursor = ts.QueryCursor(query)
    matches = cursor.matches(tree.root_node)

    result = ExtractionResult(language=language)

    # node.id -> local_id, populated as entities are created so later
    # tiers (and same-tier siblings processed after) can find their
    # structural parent regardless of source order.
    node_id_to_local: dict[int, int] = {}
    # kind-prefix -> node.id -> raw capture bundle, deduplicated so a node
    # matched by more than one pattern for the same kind is not double-
    # counted (see e.g. Rust's trait vs impl function patterns).
    raw_by_kind: dict[str, dict[int, dict[str, list[ts.Node]]]] = {
        kind: {} for kind in _KIND_BY_PREFIX
    }

    for _pattern_index, captures in matches:
        for capture_name, nodes in captures.items():
            if not capture_name.endswith(".definition"):
                continue
            prefix = capture_name[: -len(".definition")]
            if prefix not in _KIND_BY_PREFIX:
                continue
            for def_node in nodes:
                raw_by_kind[prefix].setdefault(def_node.id, captures)

    entities: list[ExtractedEntity] = []
    namespace_local_id: int | None = None

    if not raw_by_kind["namespace"]:
        # No explicit namespace/package capture (Python, JS/TS, Rust):
        # synthesize one from the file's path *before* any other entity,
        # so it lands at local_id 0 -- everything else's fallback parent
        # link (below) can then point at an already-existing row instead
        # of a forward reference, which entities.parent_id's foreign key
        # would otherwise reject at insert time.
        namespace_local_id = 0
        entities.append(
            ExtractedEntity(
                local_id=0,
                kind=EntityType.NAMESPACE,
                name=default_namespace_name,
                qualified_name=default_namespace_qualified_name,
                parent_local_id=None,
                signature=None,
                start_line=1,
                end_line=max(1, source.count(b"\n") + 1),
                start_col=0,
                end_col=0,
            )
        )

    for tier in _TIER_ORDER:
        # Deterministic order within a tier: source position, independent
        # of which query pattern happened to produce the match first.
        pending: list[tuple[str, ts.Node, dict[str, list[ts.Node]]]] = []
        for prefix in sorted(tier):
            for def_node_id, captures in raw_by_kind[prefix].items():
                def_nodes = captures[f"{prefix}.definition"]
                def_node = next(n for n in def_nodes if n.id == def_node_id)
                pending.append((prefix, def_node, captures))
        pending.sort(key=lambda item: item[1].start_byte)

        for prefix, def_node, captures in pending:
            name_nodes = captures.get(f"{prefix}.name")
            if not name_nodes:
                continue
            name_text = _decode(source, name_nodes[0])
            parent_name_nodes = captures.get(f"{prefix}.parent_name")

            kind = _KIND_BY_PREFIX[prefix]
            parent_local_id: int | None
            if parent_name_nodes:
                parent_name_text = _decode(source, parent_name_nodes[0])
                parent_local_id = next(
                    (
                        e.local_id
                        for e in entities
                        if e.kind in _TYPE_LIKE and e.name == parent_name_text
                    ),
                    None,
                )
            else:
                parent_local_id = _nearest_ancestor_entity(def_node, node_id_to_local)
            if parent_local_id is None and namespace_local_id is not None:
                parent_local_id = namespace_local_id

            if kind is EntityType.FUNCTION and parent_local_id is not None:
                parent_kind = entities[parent_local_id].kind
                if parent_kind in _TYPE_LIKE:
                    kind = EntityType.METHOD

            parent_qualified_name = (
                entities[parent_local_id].qualified_name if parent_local_id is not None else None
            )
            qualified_name = (
                f"{parent_qualified_name}.{name_text}" if parent_qualified_name else name_text
            )

            local_id = len(entities)
            entities.append(
                ExtractedEntity(
                    local_id=local_id,
                    kind=kind,
                    name=name_text,
                    qualified_name=qualified_name,
                    parent_local_id=parent_local_id,
                    signature=_signature(source, def_node),
                    start_line=def_node.start_point[0] + 1,
                    end_line=def_node.end_point[0] + 1,
                    start_col=def_node.start_point[1],
                    end_col=def_node.end_point[1],
                )
            )
            node_id_to_local[def_node.id] = local_id
            if prefix == "namespace" and namespace_local_id is None:
                namespace_local_id = local_id

    result.entities = entities

    for _pattern_index, captures in matches:
        if "import.module" in captures:
            module_node = captures["import.module"][0]
            result.imports.append(
                ExtractedImport(
                    module=_strip_quotes(_decode(source, module_node)),
                    line=module_node.start_point[0] + 1,
                )
            )
        if "call.callee" in captures and "call.expression" in captures:
            callee_node = captures["call.callee"][0]
            call_node = captures["call.expression"][0]
            result.calls.append(
                ExtractedCall(
                    caller_local_id=_nearest_ancestor_entity(call_node, node_id_to_local),
                    callee_text=_decode(source, callee_node),
                    line=call_node.start_point[0] + 1,
                )
            )
        for verb, rel_type in (
            ("extends", RelationshipType.EXTENDS),
            ("implements", RelationshipType.IMPLEMENTS),
        ):
            object_nodes = captures.get(f"{verb}.object")
            if not object_nodes:
                continue
            object_node = object_nodes[0]
            subject_nodes = captures.get(f"{verb}.subject")
            if subject_nodes:
                result.inherits.append(
                    ExtractedInherit(
                        relationship_type=rel_type,
                        subject_local_id=None,
                        subject_name=_decode(source, subject_nodes[0]),
                        object_name=_decode(source, object_node),
                        line=object_node.start_point[0] + 1,
                    )
                )
            else:
                subject_local_id = _nearest_ancestor_entity(object_node, node_id_to_local)
                if subject_local_id is None:
                    continue
                result.inherits.append(
                    ExtractedInherit(
                        relationship_type=rel_type,
                        subject_local_id=subject_local_id,
                        subject_name=None,
                        object_name=_decode(source, object_node),
                        line=object_node.start_point[0] + 1,
                    )
                )
        if "decorator.raw" in captures and "decorator.subject" in captures:
            subject_node = captures["decorator.subject"][0]
            subject_local_id = node_id_to_local.get(subject_node.id)
            if subject_local_id is None:
                continue
            raw_node = captures["decorator.raw"][0]
            result.decorators.append(
                ExtractedDecorator(
                    subject_local_id=subject_local_id,
                    text=_decode(source, raw_node),
                    line=raw_node.start_point[0] + 1,
                )
            )

    return result


__all__ = [
    "ExtractedCall",
    "ExtractedDecorator",
    "ExtractedEntity",
    "ExtractedImport",
    "ExtractedInherit",
    "ExtractionResult",
    "default_namespace_for_path",
    "extract",
]
