"""``LocalKnowledgeBackend`` -- a ``KnowledgeBackend`` wrapper around the
existing local SQLite+FTS5+USearch stack.

Storage backend abstraction plan, Phase 3: the write half of the contract
(``upsert_file``/``delete_file``/``publish_code``/``publish_document``/
``publish_embeddings``/``publish_links``) is now real, moved here from
``code/processor.py``, ``documents/pipeline.py``,
``indexing/embedding_indexer.py`` and ``knowledge/linker.py`` -- see each
method's docstring for exactly which pre-Phase-3 function's write half it
replaces. This is a faithful relocation, not a rewrite: same tables, same
transactions (a publish method never opens its own transaction --
*callers* -- the coordinator's/runner's/linker's single writer connection
-- wrap the call in ``with transaction(conn):`` exactly as they wrapped
the direct repository calls before), same generation/delete-then-insert
semantics.

Two constructor shapes:

- ``LocalKnowledgeBackend(home=...)`` (Phase 1's shape, unchanged): opens
  its own connection to ``sources.db`` lazily and migrates the
  ``"sources"`` schema group. Used by ``backends.factory.create_backend``
  for a fresh, standalone backend, and by every pre-Phase-3 test.
- ``LocalKnowledgeBackend(conn=existing_conn)``: binds to an
  *externally-owned* connection instead -- what ``indexing/runner.py``
  passes (the same per-project ``knowledge.db`` connection
  ``IndexCoordinator``/``link_touched_files``/``publish_embeddings``
  already write through via ``AppContext.project_conn``), so a publish
  call lands in the exact same connection/transaction the rest of a
  source pass uses. ``close()`` never closes an externally-owned
  connection -- its lifecycle stays the caller's.

Reads/search (``lexical_search`` etc.) and the generation lifecycle
methods are unchanged from Phase 1 -- still explicit
``NotImplementedError`` stubs; routing retrieval and the local
rebuild-safety story through this contract is out of this phase's scope
(see the plan's Phase 3 write-up).

Imports here stay within the existing local stack
(``ragmonk.storage``/``ragmonk.core``/``ragmonk.documents.chunker``);
nothing server-specific is imported, directly or transitively.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.models import (
    BackendStats,
    DocumentRecord,
    DocumentUnitRecord,
    FileRecord,
    LinkRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)
from ragmonk.core import paths
from ragmonk.core.models import CrossLink, Entity, Paragraph, Section, Table
from ragmonk.documents.chunker import Chunk
from ragmonk.storage.migrations import apply_migrations
from ragmonk.storage.repositories import (
    documents_repo,
    embedding_cache_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    links_repo,
    relationships_repo,
    vector_items_repo,
)
from ragmonk.storage.sqlite import connect


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LocalKnowledgeBackend(KnowledgeBackend):
    """Wraps a local SQLite connection's lifecycle -- either its own,
    lazily opened against ``sources.db`` under ``home``, or an externally
    supplied one (see this module's docstring).
    """

    def __init__(
        self,
        *,
        home: Path | None = None,
        cache_size_mb: int = 64,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self._home = home or paths.runtime_dir()
        self._cache_size_mb = cache_size_mb
        self._external_conn = conn is not None
        self._conn: sqlite3.Connection | None = conn

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        """Real, minimal check: open (or reuse) the connection and run a
        trivial query.
        """
        try:
            conn = self._connection()
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def ensure_schema(self) -> None:
        conn = self._connection()
        # An externally-owned connection is always the per-project
        # ``knowledge.db`` (see this module's docstring) -- already
        # migrated by whoever created it (``AppContext.project_conn``),
        # but re-running ``apply_migrations`` is an idempotent no-op, so
        # this stays correct even for a caller that supplies its own
        # not-yet-migrated connection.
        apply_migrations(conn, "knowledge" if self._external_conn else "sources")

    def close(self) -> None:
        if self._conn is not None and not self._external_conn:
            self._conn.close()
        self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = connect(
                paths.sources_db_path(self._home), cache_size_mb=self._cache_size_mb
            )
        return self._conn

    # -- generation lifecycle -------------------------------------------
    # Not yet mapped: today's local rebuild-safety story is implemented
    # per-repository inside ``indexing/coordinator.py`` and
    # ``storage/repositories``, not as a single named "generation" handle.
    # Reusing that machinery here is a future phase's job.
    def begin_generation(self, source_id: str) -> str:
        raise NotImplementedError(
            "LocalKnowledgeBackend.begin_generation: local rebuild-safety is not yet "
            "wired through the KnowledgeBackend contract (future phase)"
        )

    def publish_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.publish_generation: not yet wired (future phase)"
        )

    def abort_generation(self, source_id: str, generation: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.abort_generation: not yet wired (future phase)"
        )

    # -- writes -----------------------------------------------------------
    # Storage backend abstraction plan, Phase 3: ``upsert_file``/
    # ``delete_file`` intentionally stay narrow -- ``files_repo`` also
    # backs ``IndexCoordinator``'s scan/classify/retry/rename state
    # machine (``FileStatus`` transitions, job claiming, generation
    # bumps on ``mark_indexed``/``mark_failed``), which is not "final
    # persistence of parsed content" the way entities/document units/
    # embeddings/links are, and reusing this contract's minimal
    # ``FileRecord`` for all of that would force a much larger rewrite
    # this phase's own "be conservative" guidance argues against (see
    # ``IndexCoordinator``, unchanged by this phase). These two methods
    # cover the one part of file-record persistence that *does* map
    # cleanly: recording a file's identity/content hash, and removing it
    # -- callers needing the fuller lifecycle keep using ``files_repo``
    # directly, exactly as before this phase.
    def upsert_file(self, file_record: FileRecord) -> None:
        conn = self._connection()
        existing = files_repo.get(conn, file_record.file_id)
        now = _now()
        if existing is None:
            from ragmonk.core.models import FileKind, FileStatus
            from ragmonk.core.models import FileRecord as CoreFileRecord
            from ragmonk.sources import detector

            files_repo.insert(
                conn,
                CoreFileRecord(
                    id=file_record.file_id,
                    source_id=file_record.source_id,
                    path=file_record.path,
                    kind=detector.classify(Path(file_record.path)) or FileKind.UNKNOWN,
                    size=file_record.size_bytes,
                    mtime=file_record.mtime or 0.0,
                    content_hash=file_record.content_hash,
                    status=FileStatus.INDEXED,
                    generation=0,
                    created_at=now,
                    updated_at=now,
                ),
            )
        else:
            files_repo.update_status(
                conn,
                file_record.file_id,
                existing.status,
                updated_at=now,
                size=file_record.size_bytes,
                mtime=file_record.mtime,
                content_hash=file_record.content_hash,
            )

    def delete_file(self, source_id: str, file_id: str) -> None:
        files_repo.delete(self._connection(), file_id)

    def publish_code(self, prepared_code: PreparedCode) -> None:
        """The write half of ``code.processor.publish_code`` (Phase 2):
        atomically replaces ``prepared_code.file_id``'s previous
        generation's entities/relationships with the ones supplied here.
        Cross-file symbol resolution (building ``prepared_code.
        relationships`` in the first place) stays in
        ``code.processor.publish_code`` -- a read against the live
        project database, not a write, so it is out of this method's
        scope exactly like retrieval/search is (see this module's
        docstring and the plan's "reads still go direct" scoping).
        """
        conn = self._connection()
        entities_repo.delete_by_file(conn, prepared_code.file_id)
        if prepared_code.clear_only:
            return
        for entity in prepared_code.entities:
            snippet = prepared_code.entity_snippets.get(entity.id, entity.name)
            entities_repo.insert(conn, entity, snippet=snippet)
        for relationship in prepared_code.relationships:
            relationships_repo.insert(conn, relationship)

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        """The write half of ``documents.pipeline.publish_document``
        (Phase 3/pre-refactor): atomically replaces
        ``prepared_document.file_id``'s previous generation's document/
        sections/paragraphs/tables with the ones supplied here.
        """
        conn = self._connection()
        documents_repo.delete_by_file(conn, prepared_document.file_id)
        if prepared_document.delete_only or prepared_document.document is None:
            return

        document = prepared_document.document
        documents_repo.insert_document(conn, document)
        doc_title = prepared_document.doc_title
        for index, (chunk_id, chunk) in enumerate(
            zip(prepared_document.chunk_ids, prepared_document.chunks, strict=True)
        ):
            parent_id = (
                prepared_document.chunk_ids[chunk.parent_index]
                if chunk.parent_index is not None
                else None
            )
            _insert_chunk(
                conn,
                chunk_id=chunk_id,
                document_id=document.id,
                file_id=prepared_document.file_id,
                parent_id=parent_id,
                order_index=index,
                chunk=chunk,
                generation=prepared_document.generation,
                created_at=document.created_at,
                doc_title=doc_title,
            )

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        """The write half of ``indexing.embedding_indexer.
        publish_embeddings`` (Phase 9/P5): replaces every touched file's
        previous embeddings generation with ``prepared_embeddings``'s
        already-computed vectors, stamps each touched file's embedding
        reuse identity, and upserts the persistent embedding-reuse
        cache. No model inference here, mirroring the function this
        replaces -- run inside the caller's transaction.
        """
        conn = self._connection()
        now = _now()
        touched_files = list(
            prepared_embeddings.touched_code_file_ids
            | prepared_embeddings.touched_document_file_ids
        )

        embeddings_repo.delete_by_files(conn, touched_files)
        vector_items_repo.delete_by_files(conn, touched_files)

        if prepared_embeddings.touched_code_file_ids:
            files_repo.update_embedding_version_many(
                conn,
                list(prepared_embeddings.touched_code_file_ids),
                embedding_model_id=prepared_embeddings.model_id,
                embedding_text_version=prepared_embeddings.code_embedding_text_version,
                updated_at=now,
            )
        if prepared_embeddings.touched_document_file_ids:
            files_repo.update_embedding_version_many(
                conn,
                list(prepared_embeddings.touched_document_file_ids),
                embedding_model_id=prepared_embeddings.model_id,
                embedding_text_version=prepared_embeddings.document_embedding_text_version,
                updated_at=now,
            )

        for (subject_type, subject_id, file_id, _text), vector in zip(
            prepared_embeddings.subjects, prepared_embeddings.vectors, strict=True
        ):
            embeddings_repo.insert(
                conn,
                subject_type=subject_type,
                subject_id=subject_id,
                file_id=file_id,
                source_id=prepared_embeddings.source_id,
                model_id=prepared_embeddings.model_id,
                vector=vector,
            )
            vector_items_repo.insert(
                conn,
                subject_type=subject_type.value,
                subject_id=subject_id,
                file_id=file_id,
                source_id=prepared_embeddings.source_id,
                model_id=prepared_embeddings.model_id,
            )

        from ragmonk.tokenization import model_identity

        preprocessing_version = model_identity.preprocessing_fingerprint()
        for hashed_text, embedding_text_version, vector in prepared_embeddings.cache_entries:
            embedding_cache_repo.put(
                conn,
                hashed_text,
                model_id=prepared_embeddings.model_id,
                preprocessing_version=preprocessing_version,
                embedding_text_version=embedding_text_version,
                vector=vector,
                created_at=now,
            )

    def publish_links(self, prepared_links: PreparedLinks) -> int:
        """The write half of ``knowledge.linker.link_touched_files``:
        inserts ``prepared_links.candidates`` as ``CrossLink`` rows,
        deduplicating via the same natural-key uniqueness
        ``links_repo.insert`` already enforces, and returns how many
        were newly inserted.
        """
        conn = self._connection()
        now = _now()
        inserted = 0
        for candidate in prepared_links.candidates:
            link = CrossLink(
                id=uuid.uuid4().hex,
                link_type=candidate.link_type,
                entity_id=candidate.entity_id,
                document_id=candidate.document_id,
                section_id=candidate.section_id,
                resolver=candidate.resolver,
                confidence=candidate.confidence,
                evidence=candidate.evidence,
                created_at=now,
            )
            if links_repo.insert(conn, link):
                inserted += 1
        return inserted

    # -- reads / search ---------------------------------------------------
    # Not yet mapped: today's search entry points live in
    # ``retrieval/lexical.py``/``semantic.py``/``graph.py`` and return
    # their own result types, not ``SearchHit``. Routing them through here
    # is a future phase.
    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.lexical_search: not yet wired (future phase); "
            "use ragmonk.retrieval.lexical directly for now"
        )

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.semantic_search: not yet wired (future phase); "
            "use ragmonk.retrieval.semantic directly for now"
        )

    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.symbol_search: not yet wired (future phase)"
        )

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.graph_neighbors: not yet wired (future phase); "
            "use ragmonk.retrieval.graph directly for now"
        )

    def get_file(self, file_id: str) -> FileRecord | None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_file: not yet wired (future phase)"
        )

    def get_entities_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_entities_for_files: not yet wired (future phase)"
        )

    def get_document_units_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "LocalKnowledgeBackend.get_document_units_for_files: not yet wired "
            "(future phase)"
        )

    def count_stats(self) -> BackendStats:
        raise NotImplementedError(
            "LocalKnowledgeBackend.count_stats: not yet wired (future phase)"
        )

    # -- Completion plan F4: targeted read primitives ----------------------
    # Thin projections over the existing repositories, against this
    # backend's own (per-project) connection -- the local-mode side of
    # the same contract the server adapters answer from the engine.
    def get_files(self, file_ids: list[str]) -> list[FileRecord]:
        records = files_repo.get_many(self._connection(), list(file_ids))
        return [_file_record(records[fid]) for fid in file_ids if fid in records]

    def list_files(self, source_id: str) -> list[FileRecord]:
        return [_file_record(r) for r in files_repo.list_by_source(self._connection(), source_id)]

    def get_entities(self, entity_ids: list[str]) -> list[Entity]:
        conn = self._connection()
        out: list[Entity] = []
        for entity_id in dict.fromkeys(entity_ids):
            entity = entities_repo.get(conn, entity_id)
            if entity is not None:
                out.append(entity)
        return out

    def list_entities(
        self, *, source_id: str | None = None, query: str | None = None, limit: int = 100
    ) -> list[Entity]:
        conn = self._connection()
        if query:
            try:
                entities = entities_repo.search_fts(conn, query, limit=limit)
            except sqlite3.OperationalError:
                entities = []
        else:
            entities = entities_repo.list_all(conn)
        if source_id:
            entities = [e for e in entities if e.source_id == source_id]
        return entities[:limit]

    def list_source_entities(
        self, source_id: str, *, generation: str | None = None
    ) -> list[Entity]:
        return [e for e in entities_repo.list_all(self._connection()) if e.source_id == source_id]

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        conn = self._connection()
        found: dict[str, Entity] = {}
        for name in names or []:
            for entity in entities_repo.find_by_name(conn, name):
                found.setdefault(entity.id, entity)
        for qualified in qualified_names or []:
            for entity in entities_repo.find_by_qualified_name(conn, qualified):
                found.setdefault(entity.id, entity)
        out = list(found.values())
        if source_id:
            out = [e for e in out if e.source_id == source_id]
        return out

    def get_links(
        self,
        *,
        entity_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[LinkRecord]:
        conn = self._connection()
        seen: set[str] = set()
        out: list[LinkRecord] = []
        links: list[CrossLink] = []
        for entity_id in entity_ids or []:
            links.extend(links_repo.list_by_entity(conn, entity_id))
        for document_id in document_ids or []:
            links.extend(links_repo.list_by_document(conn, document_id))
        for link in links:
            if link.id in seen:
                continue
            seen.add(link.id)
            out.append(
                LinkRecord(
                    entity_id=link.entity_id,
                    document_id=link.document_id,
                    section_id=link.section_id,
                    link_type=link.link_type.value,
                    resolver=link.resolver,
                    confidence=link.confidence.value,
                    evidence=link.evidence or "",
                    id=link.id,
                )
            )
        return out

    def remove_link(self, link_id: str) -> bool:
        return links_repo.delete(self._connection(), link_id)

    def get_documents(self, document_ids: list[str]) -> list[DocumentRecord]:
        conn = self._connection()
        out: list[DocumentRecord] = []
        for document_id in dict.fromkeys(document_ids):
            document = documents_repo.get_document(conn, document_id)
            if document is not None:
                out.append(_document_record(document))
        return out

    def list_documents(
        self, *, source_id: str | None = None, limit: int | None = None
    ) -> list[DocumentRecord]:
        conn = self._connection()
        documents = (
            documents_repo.list_by_source(conn, source_id)
            if source_id
            else documents_repo.list_all(conn)
        )
        records = [_document_record(d) for d in documents]
        return records if limit is None else records[:limit]

    def get_document_units(
        self,
        *,
        document_id: str | None = None,
        unit_ids: list[str] | None = None,
    ) -> list[DocumentUnitRecord]:
        conn = self._connection()
        if document_id is not None:
            document = documents_repo.get_document(conn, document_id)
            if document is None:
                return []
            units = documents_repo.list_units_by_file(conn, document.file_id)
            return [_unit_record(u, document.source_id) for u in units]
        out: list[DocumentUnitRecord] = []
        for unit_id in dict.fromkeys(unit_ids or []):
            unit = documents_repo.get_unit(conn, unit_id)
            if unit is not None:
                out.append(_unit_record(unit, ""))
        return out

    def list_source_document_units(
        self, source_id: str, *, generation: str | None = None
    ) -> list[DocumentUnitRecord]:
        conn = self._connection()
        file_ids = {f.id for f in files_repo.list_by_source(conn, source_id)}
        return [
            _unit_record(u, source_id)
            for u in documents_repo.list_all_units(conn)
            if u.file_id in file_ids
        ]

    def find_relationships_by_target_prefix(
        self, source_id: str, prefix: str, *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        return [
            r.model_dump(mode="json")
            for r in relationships_repo.find_by_target_symbol_prefix(self._connection(), prefix)
        ]

    def clear_source(self, source_id: str) -> None:
        raise NotImplementedError(
            "LocalKnowledgeBackend.clear_source: not yet wired (future phase)"
        )


def _file_record(record: Any) -> FileRecord:
    return FileRecord(
        file_id=record.id,
        source_id=record.source_id,
        path=record.path,
        content_hash=record.content_hash or "",
        size_bytes=record.size,
        mtime=record.mtime,
        metadata={
            "kind": record.kind.value,
            "status": record.status.value,
            "last_indexed_at": record.last_indexed_at,
            "updated_at": record.updated_at,
        },
    )


def _document_record(document: Any) -> DocumentRecord:
    return DocumentRecord(
        document_id=document.id,
        source_id=document.source_id,
        file_id=document.file_id,
        title=document.title or "",
        format=document.format.value,
        author=document.author,
        page_count=document.page_count,
        section_count=document.section_count,
        paragraph_count=document.paragraph_count,
        table_count=document.table_count,
        is_scanned=document.is_scanned,
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def _unit_record(unit: Any, source_id: str) -> DocumentUnitRecord:
    return DocumentUnitRecord(
        unit_id=unit.id,
        document_id=unit.document_id,
        file_id=unit.file_id,
        source_id=source_id,
        kind=unit.kind.value,
        text=unit.text,
        heading_path=list(unit.heading_path),
        page_start=unit.page_start,
        page_end=unit.page_end,
        embedding_text=unit.embedding_text,
        has_embedding=bool(unit.embedding_text),
    )


def _insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    document_id: str,
    file_id: str,
    parent_id: str | None,
    order_index: int,
    chunk: Chunk,
    generation: int,
    created_at: str,
    doc_title: str,
) -> None:
    """Byte-for-byte the write ``documents.pipeline._insert_chunk`` used
    to perform directly -- one row into ``document_sections`` per chunk
    kind, moved here as part of Phase 3's persistence-boundary move.
    """
    heading_path = list(chunk.heading_path)
    if chunk.kind == "heading":
        documents_repo.insert_section(
            conn,
            Section(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                heading_level=chunk.heading_level or 0,
                text=chunk.text,
                heading_path=heading_path,
                parent_id=parent_id,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
        )
    elif chunk.kind == "table":
        rows = [list(row) for row in (chunk.table_rows or ())]
        documents_repo.insert_table(
            conn,
            Table(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                heading_path=heading_path,
                parent_id=parent_id,
                rows=rows,
                num_rows=len(rows),
                num_cols=len(rows[0]) if rows else 0,
                caption=chunk.caption,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
        )
    else:
        documents_repo.insert_paragraph(
            conn,
            Paragraph(
                id=chunk_id,
                document_id=document_id,
                file_id=file_id,
                text=chunk.text,
                heading_path=heading_path,
                parent_id=parent_id,
                order_index=order_index,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                generation=generation,
                created_at=created_at,
            ),
            doc_title=doc_title,
            search_text=chunk.search_text,
            embedding_text=chunk.contextual_text,
        )
