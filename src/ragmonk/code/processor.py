"""``CodeProcessor``: the Phase 2 processor registered against
``FileKind.CODE`` in ``indexing.coordinator.ProcessorRegistry``.

Ties together parsing, extraction, resolution and framework heuristics,
and performs the atomic "delete previous generation, insert new entities/
relationships/FTS rows" step described in ``storage/schema.py``. Anything
this raises (a genuine syntax error, an unreadable file, ...) is left to
propagate: ``IndexCoordinator._process_queue`` already catches, records
and retries/fails processor exceptions per file without aborting the run
-- Phase 2 does not need its own copy of that logic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ragmonk.code import framework_rules
from ragmonk.code.extractor import ExtractionResult, default_namespace_for_path, extract
from ragmonk.code.parser import detect_language, parse
from ragmonk.code.resolver import ResolvedTarget, resolve_reference
from ragmonk.core.errors import RagMonkError
from ragmonk.core.models import Confidence, Entity, FileStatus, Relationship, RelationshipType
from ragmonk.indexing.coordinator import ProcessingOutcome, ProcessorContext
from ragmonk.storage.repositories import entities_repo, relationships_repo
from ragmonk.storage.sqlite import transaction

EntityLookup = Callable[[str], list[Entity]]


class CodeParseError(RagMonkError):
    """A source file's syntax could not be parsed by its Tree-sitter grammar.

    Tree-sitter itself does not raise on malformed input -- it produces an
    error-recovered tree -- so this is raised explicitly after parsing
    when the tree contains an error node, giving poisoned/truncated
    source files the same fail-and-isolate treatment as any other
    processor exception (see coordinator._process_queue).
    """


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _relative_posix_path(path: Path, root: Path) -> PurePosixPath:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(path.name)
    return PurePosixPath(rel.as_posix())


def _decorator_head(text: str) -> str:
    """The callable/attribute name portion of a decorator/attribute's
    source text, e.g. ``'@app.route("/x")'`` -> ``'app.route'`` -- the
    part meaningful to pass to the resolver as a symbol name.
    """
    stripped = text.lstrip("@").strip()
    head = stripped.split("(", 1)[0]
    return head.strip()


def code_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None or ctx.source_root is None:
        raise RagMonkError("CodeProcessor requires a coordinator-provided ProcessorContext")

    language = detect_language(ctx.path)
    if language is None:
        # A recognized "code" extension (per sources.detector) that Phase 2
        # has no grammar for yet (e.g. .rb, .sql, .sh) -- index the file
        # without entities rather than failing the run.
        with transaction(ctx.conn):
            entities_repo.delete_by_file(ctx.conn, ctx.file_id)
        return ProcessingOutcome(status=FileStatus.INDEXED)

    source = ctx.path.read_bytes()
    tree = parse(source, language)
    if tree.root_node.has_error:
        raise CodeParseError(f"{ctx.path}: syntax error(s) in a {language} file")

    relative = _relative_posix_path(ctx.path, ctx.source_root)
    default_name, default_qualified_name = default_namespace_for_path(relative)
    extraction = extract(
        source,
        language,
        default_namespace_name=default_name,
        default_namespace_qualified_name=default_qualified_name,
    )

    now = _now()
    entities: list[Entity] = []
    for raw_entity in extraction.entities:
        entities.append(
            Entity(
                id=uuid.uuid4().hex,
                source_id=ctx.source_id,
                file_id=ctx.file_id,
                kind=raw_entity.kind,
                name=raw_entity.name,
                qualified_name=raw_entity.qualified_name,
                language=language,
                parent_id=None,
                signature=raw_entity.signature,
                start_line=raw_entity.start_line,
                end_line=raw_entity.end_line,
                start_col=raw_entity.start_col,
                end_col=raw_entity.end_col,
                generation=ctx.next_generation,
                created_at=now,
                updated_at=now,
            )
        )
    for local_id, raw_entity in enumerate(extraction.entities):
        if raw_entity.parent_local_id is not None:
            entities[local_id] = entities[local_id].model_copy(
                update={"parent_id": entities[raw_entity.parent_local_id].id}
            )

    namespace_local_id = next(
        i for i, e in enumerate(extraction.entities) if e.kind.value == "namespace"
    )

    with transaction(ctx.conn):
        entities_repo.delete_by_file(ctx.conn, ctx.file_id)
        for local_id, entity in enumerate(entities):
            snippet = extraction.entities[local_id].signature or entity.name
            entities_repo.insert(ctx.conn, entity, snippet=snippet)

        conn = ctx.conn
        this_file_id = ctx.file_id

        def qualified_lookup(text: str) -> list[Entity]:
            return [
                e for e in entities_repo.find_by_qualified_name(conn, text)
                if e.file_id != this_file_id
            ]

        def name_lookup(name: str) -> list[Entity]:
            return [
                e for e in entities_repo.find_by_name(conn, name) if e.file_id != this_file_id
            ]

        relationships = _build_relationships(
            ctx=ctx,
            extraction=extraction,
            entities=entities,
            namespace_local_id=namespace_local_id,
            language=language,
            now=now,
            qualified_lookup=qualified_lookup,
            name_lookup=name_lookup,
        )
        for relationship in relationships:
            relationships_repo.insert(ctx.conn, relationship)

    return ProcessingOutcome(status=FileStatus.INDEXED)


def _build_relationships(
    *,
    ctx: ProcessorContext,
    extraction: ExtractionResult,
    entities: list[Entity],
    namespace_local_id: int,
    language: str,
    now: str,
    qualified_lookup: EntityLookup,
    name_lookup: EntityLookup,
) -> list[Relationship]:
    assert ctx.file_id is not None
    file_id = ctx.file_id
    generation = ctx.next_generation
    relationships: list[Relationship] = []

    def new_relationship(
        *,
        relationship_type: RelationshipType,
        source_entity_id: str,
        target: ResolvedTarget,
        line: int,
        evidence: str | None = None,
    ) -> Relationship:
        return Relationship(
            id=uuid.uuid4().hex,
            relationship_type=relationship_type,
            source_entity_id=source_entity_id,
            target_entity_id=target.entity_id,
            target_symbol=None if target.entity_id is not None else target.symbol,
            resolver=target.resolver,
            confidence=target.confidence,
            file_id=file_id,
            source_location=f"{ctx.path}:{line}",
            evidence=evidence,
            generation=generation,
            created_at=now,
        )

    def resolve(text: str) -> ResolvedTarget:
        return resolve_reference(
            text,
            same_file_entities=entities,
            qualified_name_lookup=qualified_lookup,
            name_lookup=name_lookup,
        )

    for raw_entity in extraction.entities:
        if raw_entity.parent_local_id is None:
            continue
        child_id = entities[raw_entity.local_id].id
        parent_id = entities[raw_entity.parent_local_id].id
        relationships.append(
            Relationship(
                id=uuid.uuid4().hex,
                relationship_type=RelationshipType.CONTAINS,
                source_entity_id=parent_id,
                target_entity_id=child_id,
                target_symbol=None,
                resolver="structural",
                confidence=Confidence.EXACT,
                file_id=file_id,
                source_location=f"{ctx.path}:{raw_entity.start_line}",
                evidence=None,
                generation=generation,
                created_at=now,
            )
        )
        relationships.append(
            Relationship(
                id=uuid.uuid4().hex,
                relationship_type=RelationshipType.DEFINED_IN,
                source_entity_id=child_id,
                target_entity_id=parent_id,
                target_symbol=None,
                resolver="structural",
                confidence=Confidence.EXACT,
                file_id=file_id,
                source_location=f"{ctx.path}:{raw_entity.start_line}",
                evidence=None,
                generation=generation,
                created_at=now,
            )
        )

    namespace_entity_id = entities[namespace_local_id].id
    for imp in extraction.imports:
        relationships.append(
            new_relationship(
                relationship_type=RelationshipType.IMPORTS,
                source_entity_id=namespace_entity_id,
                target=resolve(imp.module),
                line=imp.line,
                evidence=imp.module,
            )
        )

    for call in extraction.calls:
        caller_id = (
            entities[call.caller_local_id].id
            if call.caller_local_id is not None
            else namespace_entity_id
        )
        relationships.append(
            new_relationship(
                relationship_type=RelationshipType.CALLS,
                source_entity_id=caller_id,
                target=resolve(call.callee_text),
                line=call.line,
                evidence=call.callee_text,
            )
        )

    for inherit in extraction.inherits:
        if inherit.subject_local_id is not None:
            subject_id = entities[inherit.subject_local_id].id
        elif inherit.subject_name is not None:
            match = next((e for e in entities if e.name == inherit.subject_name), None)
            if match is None:
                continue
            subject_id = match.id
        else:
            continue
        relationships.append(
            new_relationship(
                relationship_type=inherit.relationship_type,
                source_entity_id=subject_id,
                target=resolve(inherit.object_name),
                line=inherit.line,
                evidence=inherit.object_name,
            )
        )

    for decorator in extraction.decorators:
        subject_id = entities[decorator.subject_local_id].id
        relationships.append(
            new_relationship(
                relationship_type=RelationshipType.REFERENCES,
                source_entity_id=subject_id,
                target=resolve(_decorator_head(decorator.text)),
                line=decorator.line,
                evidence=decorator.text,
            )
        )

    for finding in framework_rules.detect(language, extraction.decorators):
        subject_id = entities[finding.subject_local_id].id
        relationships.append(
            Relationship(
                id=uuid.uuid4().hex,
                relationship_type=finding.relationship_type,
                source_entity_id=subject_id,
                target_entity_id=None,
                target_symbol=finding.target_symbol,
                resolver=finding.resolver,
                confidence=finding.confidence,
                file_id=file_id,
                source_location=f"{ctx.path}",
                evidence=finding.evidence,
                generation=generation,
                created_at=now,
            )
        )

    return relationships
