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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ragmonk.backends.models import PreparedCode as BackendPreparedCode
from ragmonk.code import framework_rules
from ragmonk.code.extractor import ExtractionResult, default_namespace_for_path, extract
from ragmonk.code.parser import detect_language, parse
from ragmonk.code.resolver import ResolvedTarget, resolve_reference
from ragmonk.core.errors import ContentChangedDuringProcessingError, RagMonkError
from ragmonk.core.models import Confidence, Entity, FileStatus, Relationship, RelationshipType
from ragmonk.indexing.coordinator import ProcessingOutcome, ProcessorContext
from ragmonk.indexing.incremental import VersionStamp
from ragmonk.sources.fingerprint import stat_unchanged
from ragmonk.storage.repositories import entities_repo
from ragmonk.storage.sqlite import transaction

EntityLookup = Callable[[str], list[Entity]]

# Indexing optimization plan V2, Phase P1: the code parser/extractor's own
# derivation identity, independent of a source file's content_hash. Bump
# this string whenever a change to ``code/parser.py``, ``code/extractor.py``,
# ``code/resolver.py`` or ``code/framework_rules.py`` could produce
# different entities/relationships/code_fts for a file whose *bytes* did
# not change (a Tree-sitter grammar upgrade, an extraction heuristic fix,
# a new relationship kind, ...). ``code_version_stamp`` below is compared
# against a previously-indexed file's stored ``parser_version`` column
# (``indexing/incremental.decide_reprocessing``) the same way the document
# pipeline's own ``parser_version``/``chunker_version`` already are; a
# mismatch forces exactly the same full reprocess a genuinely CHANGED file
# gets, once, even though ``classify_change`` alone would have called the
# file UNCHANGED.
CODE_DERIVATION_VERSION = "1"


def code_version_stamp() -> VersionStamp:
    """The code pipeline's current derivation identity (see
    ``CODE_DERIVATION_VERSION``'s docstring above).

    ``chunker_version`` has no code-side equivalent -- Tree-sitter always
    reparses the *whole* file from source on any genuinely CHANGED file
    already, so there is no separate "chunk boundary" axis distinct from
    content_hash the way a document's chunker has -- so it stays ``None``
    (``VersionStamp``'s docstring: ``None`` on the *current* side means
    "this kind has no such axis", never itself grounds for a rebuild).

    ``embedding_model_id``/``embedding_text_version`` are deliberately
    left ``None`` here too: code embedding invalidation already has its
    own narrower path (``indexing/embedding_indexer.py``'s
    ``CODE_EMBEDDING_TEXT_VERSION``), kept independent of this structural
    reprocess so a model-only change never forces a full re-parse/
    re-extract of every code file, only a re-embed.
    """
    return VersionStamp(
        parser_version=CODE_DERIVATION_VERSION,
        chunker_version=None,
        embedding_model_id=None,
        embedding_text_version=None,
    )


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


@dataclass(frozen=True)
class PreparedCode:
    """The pure, read-only output of code extraction -- everything
    ``code_processor`` used to compute before touching the database,
    now factored out so the coordinator's bounded parallel path
    (indexing optimization plan, Phase P4) can run this half of the
    work concurrently across files in a thread pool while the
    transactional write half (``publish_code``) stays on the single
    writer thread. ``language=None`` means "a recognized code
    extension with no grammar yet" (e.g. ``.rb``, ``.sql``, ``.sh``) --
    the same "index with no entities" fallback ``code_processor``
    always had, just carried through this split instead of handled
    inline.
    """

    language: str | None
    extraction: ExtractionResult | None = None
    namespace_local_id: int | None = None


def prepare_code(path: Path, source_root: Path) -> PreparedCode:
    """Reads and parses ``path`` and extracts raw entities/relationships
    -- no database access at all, safe to call concurrently across
    files. Raises ``CodeParseError`` exactly like the pre-P4 single-
    function ``code_processor`` did; the coordinator's existing per-
    file retry/backoff handles it identically either way (see
    ``indexing/coordinator.py``'s ``_process_queue``).
    """
    language = detect_language(path)
    if language is None:
        return PreparedCode(language=None)

    source = path.read_bytes()
    tree = parse(source, language)
    if tree.root_node.has_error:
        raise CodeParseError(f"{path}: syntax error(s) in a {language} file")

    relative = _relative_posix_path(path, source_root)
    default_name, default_qualified_name = default_namespace_for_path(relative)
    extraction = extract(
        source,
        language,
        default_namespace_name=default_name,
        default_namespace_qualified_name=default_qualified_name,
    )
    namespace_local_id = next(
        i for i, e in enumerate(extraction.entities) if e.kind.value == "namespace"
    )
    return PreparedCode(
        language=language, extraction=extraction, namespace_local_id=namespace_local_id
    )


def publish_code(ctx: ProcessorContext, prepared: PreparedCode) -> ProcessingOutcome:
    """Resolves ``prepared``'s cross-file relationships and hands the
    final entities/relationships to ``KnowledgeBackend.publish_code``
    for the atomic delete-old-generation/insert-new-generation write --
    on whichever thread calls this, which the coordinator guarantees is
    always its single writer thread, never a parallel prepare worker
    (Phase P4's "one transactional publisher" rule).

    Storage backend abstraction plan, Phase 3: only the *write* moved
    behind the backend contract (see ``LocalKnowledgeBackend.
    publish_code``). ``qualified_lookup``/``name_lookup`` below still
    read ``entities_repo`` directly against ``ctx.conn`` -- a query
    against the live project database to resolve *this* file's
    relationships, not a write of authoritative state, so it stays out
    of this phase's persistence-boundary scope (mirrors retrieval/search
    staying direct -- see ``LocalKnowledgeBackend``'s module docstring).
    ``ctx.backend`` is used when the coordinator supplied one; a
    coordinator-external caller (most unit tests) gets a
    ``LocalKnowledgeBackend`` constructed on demand, bound to the same
    ``ctx.conn``, so nothing about this function's public behavior
    changes for such a caller.
    """
    assert ctx.conn is not None and ctx.file_id is not None and ctx.source_id is not None
    backend = ctx.backend
    if backend is None:
        from ragmonk.backends.local import LocalKnowledgeBackend

        backend = LocalKnowledgeBackend(conn=ctx.conn)

    if ctx.file_identity is not None:
        # Indexing optimization plan, Phase P4: a bounded parallel
        # pipeline widens the gap between when this file's content was
        # last verified (at claim/prepare time) and when it's actually
        # published -- re-check before writing so a file that changed
        # in that window is safely retried rather than silently
        # publishing content derived from stale bytes. Mirrors
        # documents/pipeline.py's identical check for the same reason.
        try:
            post_stat = ctx.path.stat()
        except OSError as exc:
            raise ContentChangedDuringProcessingError(
                f"{ctx.path}: file became unreadable during processing: {exc}"
            ) from exc
        if not stat_unchanged(
            ctx.file_identity.size, ctx.file_identity.mtime, post_stat.st_size, post_stat.st_mtime
        ):
            raise ContentChangedDuringProcessingError(
                f"{ctx.path}: file changed during processing; will be retried"
            )

    if prepared.language is None:
        # A recognized "code" extension (per sources.detector) that Phase 2
        # has no grammar for yet -- index the file without entities
        # rather than failing the run.
        with transaction(ctx.conn):
            backend.publish_code(
                BackendPreparedCode(
                    file_id=ctx.file_id, source_id=ctx.source_id, clear_only=True
                )
            )
        return ProcessingOutcome(status=FileStatus.INDEXED)

    assert prepared.extraction is not None and prepared.namespace_local_id is not None
    extraction = prepared.extraction
    language = prepared.language
    namespace_local_id = prepared.namespace_local_id

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

    conn = ctx.conn
    this_file_id = ctx.file_id

    if backend.is_server:
        # Completion plan F1: in server mode the other files' entities
        # live in the server backend (never local SQLite) -- resolve
        # cross-file symbols there, reading exactly the generation this
        # pass is writing so an in-progress rebuild resolves against its
        # own freshly written entities, not the published ones.
        server_backend = backend
        source_id = ctx.source_id
        write_generation = str(ctx.next_generation)
        cache: dict[tuple[str, str], list[Entity]] = {}

        def _server_lookup(field: str, text: str) -> list[Entity]:
            key = (field, text)
            if key not in cache:
                found = (
                    server_backend.find_entities_by_names(
                        qualified_names=[text], source_id=source_id, generation=write_generation
                    )
                    if field == "qualified_name"
                    else server_backend.find_entities_by_names(
                        names=[text], source_id=source_id, generation=write_generation
                    )
                )
                cache[key] = [
                    e for e in found
                    if e.file_id != this_file_id and getattr(e, field) == text
                ]
            return cache[key]

        def qualified_lookup(text: str) -> list[Entity]:
            return _server_lookup("qualified_name", text)

        def name_lookup(name: str) -> list[Entity]:
            return _server_lookup("name", name)

    else:

        def qualified_lookup(text: str) -> list[Entity]:
            return [
                e for e in entities_repo.find_by_qualified_name(conn, text)
                if e.file_id != this_file_id
            ]

        def name_lookup(name: str) -> list[Entity]:
            return [
                e for e in entities_repo.find_by_name(conn, name) if e.file_id != this_file_id
            ]

    entity_snippets = {
        entity.id: (extraction.entities[local_id].signature or entity.name)
        for local_id, entity in enumerate(entities)
    }

    with transaction(ctx.conn):
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
        backend.publish_code(
            BackendPreparedCode(
                file_id=ctx.file_id,
                source_id=ctx.source_id,
                generation=ctx.next_generation,
                entities=entities,
                entity_snippets=entity_snippets,
                relationships=relationships,
            )
        )

    return ProcessingOutcome(status=FileStatus.INDEXED)


def code_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    """The registered ``FileKind.CODE`` processor -- ``prepare_code``
    then ``publish_code`` run back-to-back on whichever thread calls
    this, exactly the serial, single-call behavior code_processor
    always had. The coordinator's optional bounded-parallel path calls
    ``prepare_code``/``publish_code`` directly instead (see
    ``ProcessorRegistry``'s ``prepare``/``publish`` registration), never
    through this function -- this wrapper exists only for the (default,
    ``code_extraction_workers=1``) serial path and any direct caller
    (tests, mainly) that wants the simple one-call interface.
    """
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    if ctx.conn is None or ctx.file_id is None or ctx.source_id is None or ctx.source_root is None:
        raise RagMonkError("CodeProcessor requires a coordinator-provided ProcessorContext")
    prepared = prepare_code(ctx.path, ctx.source_root)
    return publish_code(ctx, prepared)


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
