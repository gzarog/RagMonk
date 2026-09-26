"""Shared, engine-neutral read primitives and generation helpers for the
OpenSearch and Elasticsearch ``KnowledgeBackend`` adapters.

Completion plan F4/F6. Both adapters store exactly the same document
shapes in the same three indices (``{prefix}-files``/``-content``/
``-relationships`` -- see ``opensearch_mappings.py``/
``elasticsearch_mappings.py``), and differ only in *how* a query body is
sent (``body=`` for ``opensearch-py`` vs. keyword arguments for the 8.x
``elasticsearch`` client). This mixin therefore implements the targeted
read primitives (``get_entities``/``get_files``/``get_links``/
``get_documents``/...) and the generation garbage-collection helpers
once, in terms of four tiny per-engine hooks each adapter provides:

- ``_search_raw(index, query, size, sort=None, search_after=None)``
  returning the raw ``hits.hits`` list;
- ``_delete_by_query_raw(index, query)``;
- ``_index_names()`` returning ``(files, content, relationships)``;
- ``_generation_filter_clause()`` (already present on both adapters).

Nothing here imports a client SDK, and no method here ever touches local
SQLite: every read goes to the configured server engine, filtered to each
source's currently *published* generation (see ``_generation_filter_clause``
on each adapter), so an unpublished rebuild is never observable.
"""

from __future__ import annotations

from typing import Any

from ragmonk.backends.models import (
    DocumentRecord,
    DocumentUnitRecord,
    FileRecord,
    LinkRecord,
)
from ragmonk.core.models import Entity, EntityType

DEFAULT_ACTIVE_GENERATION = "0"
_SCAN_PAGE_SIZE = 1000
# Upper bound for any single targeted ``terms`` lookup batch -- engines
# cap ``terms`` at 65,536 values by default; staying well under it keeps
# every request small.
_TERMS_BATCH = 1000


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def entity_from_payload(payload: dict[str, Any]) -> Entity:
    """Rebuilds a core ``Entity`` from a stored content-index ``entity``
    document (see ``publish_code`` on either adapter).
    """
    return Entity(
        id=str(payload.get("entity_id") or ""),
        source_id=str(payload.get("source_id", "")),
        file_id=str(payload.get("file_id", "")),
        kind=EntityType(payload["kind"]),
        name=str(payload.get("name", "")),
        qualified_name=str(payload.get("qualified_name", "")),
        language=str(payload.get("language", "")),
        parent_id=payload.get("parent_id"),
        signature=payload.get("signature") or payload.get("snippet"),
        start_line=_int_or_zero(payload.get("start_line")),
        end_line=_int_or_zero(payload.get("end_line")),
        start_col=_int_or_zero(payload.get("start_col")),
        end_col=_int_or_zero(payload.get("end_col")),
        generation=_int_or_zero(payload.get("generation")),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def file_record_from_payload(payload: dict[str, Any]) -> FileRecord:
    generation = payload.get("generation")
    return FileRecord(
        file_id=str(payload["file_id"]),
        source_id=str(payload["source_id"]),
        path=str(payload.get("path", "")),
        content_hash=str(payload.get("content_hash") or ""),
        size_bytes=_int_or_zero(payload.get("size_bytes")),
        mtime=payload.get("mtime"),
        metadata=payload.get("metadata") or {},
        generation=_int_or_zero(generation) if generation is not None else None,
    )


def document_record_from_payload(payload: dict[str, Any]) -> DocumentRecord:
    meta = payload.get("doc_meta") or {}
    return DocumentRecord(
        document_id=str(payload.get("document_id", "")),
        source_id=str(payload.get("source_id", "")),
        file_id=str(payload.get("file_id", "")),
        title=str(payload.get("name") or ""),
        format=str(payload.get("kind") or ""),
        author=meta.get("author"),
        page_count=meta.get("page_count"),
        section_count=_int_or_zero(meta.get("section_count")),
        paragraph_count=_int_or_zero(meta.get("paragraph_count")),
        table_count=_int_or_zero(meta.get("table_count")),
        is_scanned=bool(meta.get("is_scanned", False)),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def unit_record_from_payload(payload: dict[str, Any]) -> DocumentUnitRecord:
    text = str(payload.get("content") or "")
    embedding_text = str(payload.get("embedding_text") or "")
    if not text:
        # A table chunk's own ``text`` is always "" (see chunker.Chunk);
        # its row-aware rendering lives in the embedding/search text.
        text = embedding_text or str(payload.get("search_text") or "")
    heading_path = payload.get("heading_path") or []
    if isinstance(heading_path, str):
        heading_path = [heading_path]
    return DocumentUnitRecord(
        unit_id=str(payload.get("chunk_id", "")),
        document_id=str(payload.get("document_id", "")),
        file_id=str(payload.get("file_id", "")),
        source_id=str(payload.get("source_id", "")),
        kind=str(payload.get("kind") or "paragraph"),
        text=text,
        heading_path=[str(h) for h in heading_path],
        page_start=payload.get("page_start"),
        page_end=payload.get("page_end"),
        embedding_text=embedding_text,
        has_embedding="embedding" in payload,
    )


def link_record_from_payload(payload: dict[str, Any]) -> LinkRecord:
    return LinkRecord(
        entity_id=str(payload.get("entity_id", "")),
        document_id=str(payload.get("document_id", "")),
        section_id=payload.get("section_id"),
        link_type=str(payload.get("relationship_type", "")),
        resolver=str(payload.get("resolver", "")),
        confidence=str(payload.get("confidence", "")),
        evidence=str(payload.get("evidence") or ""),
        source_id=str(payload.get("source_id", "")),
        id=str(payload.get("link_key", "")),
    )


def _batches(values: list[str]) -> list[list[str]]:
    unique = list(dict.fromkeys(v for v in values if v))
    return [unique[i : i + _TERMS_BATCH] for i in range(0, len(unique), _TERMS_BATCH)]


class ServerReadMixin:
    """See module docstring. Mixed into ``OpenSearchKnowledgeBackend``
    and ``ElasticsearchKnowledgeBackend`` *before* ``KnowledgeBackend`` in
    their MRO so these concrete implementations satisfy the contract.
    """

    # -- per-engine hooks (implemented by each adapter) ------------------
    def _search_raw(
        self,
        index: str,
        query: dict[str, Any],
        size: int,
        sort: list[dict[str, Any]] | None = None,
        search_after: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _delete_by_query_raw(self, index: str, query: dict[str, Any]) -> None:
        raise NotImplementedError

    def _index_names(self) -> tuple[str, str, str]:
        raise NotImplementedError

    def _generation_filter_clause(self) -> dict[str, Any]:
        raise NotImplementedError

    def _generation_marker_source(self, source_id: str) -> dict[str, Any]:
        raise NotImplementedError

    # -- generic helpers ----------------------------------------------------
    def _scan(
        self, index: str, query: dict[str, Any], sort_field: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Every matching document's ``_source``, paginated with
        ``search_after`` on a keyword ``sort_field`` so a large source is
        never silently truncated at the engine's ``max_result_window``.
        ``sort_field`` must be unique among the matched documents (every
        caller passes a per-generation-unique id field) -- ``search_after``
        on a non-unique key could skip ties across a page boundary.
        """
        out: list[dict[str, Any]] = []
        search_after: list[Any] | None = None
        sort = [{sort_field: {"order": "asc"}}]
        while True:
            page_size = _SCAN_PAGE_SIZE
            if limit is not None:
                page_size = min(page_size, limit - len(out))
                if page_size <= 0:
                    break
            hits = self._search_raw(index, query, page_size, sort=sort, search_after=search_after)
            if not hits:
                break
            out.extend(hit.get("_source", {}) for hit in hits)
            if len(hits) < page_size:
                break
            last_sort = hits[-1].get("sort")
            if not last_sort:
                break
            search_after = list(last_sort)
        return out

    def _filtered(self, *clauses: dict[str, Any], generation: str | None = None) -> dict[str, Any]:
        """``clauses`` plus the read-visibility clause: each source's
        *published* generation by default, or -- for the indexing pass
        that is itself writing ``generation`` and must read back what it
        just wrote (cross-file symbol resolution, linking, embeddings) --
        exactly that generation. Only a source-scoped writer passes one.
        """
        if generation is not None:
            return {"bool": {"filter": [*clauses, {"term": {"generation": generation}}]}}
        return {"bool": {"filter": [*clauses, self._generation_filter_clause()]}}

    # -- generation --------------------------------------------------------
    def published_generation(self, source_id: str) -> str | None:
        marker = self._generation_marker_source(source_id)
        active = marker.get("active_generation")
        if active is None or str(active) == DEFAULT_ACTIVE_GENERATION:
            return None
        return str(active)

    def _write_generation(self, source_id: str, generation: int | str | None) -> str:
        """The generation tag a write should carry: the explicit one when
        given (a generation-wrapped pass), else the source's currently
        published generation (an incremental pass updates it in place).
        """
        if generation is not None:
            return str(generation)
        return self.published_generation(source_id) or DEFAULT_ACTIVE_GENERATION

    def _gc_other_generations(self, source_id: str, keep: str) -> None:
        """After ``publish_generation`` flips the marker, delete every
        other generation's documents for ``source_id`` in every index --
        they are no longer readable (reads filter to the active
        generation), so this only reclaims space; nothing observable
        changes. The marker itself carries no ``generation`` field and is
        therefore never matched here.
        """
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"exists": {"field": "generation"}},
                ],
                "must_not": [{"term": {"generation": keep}}],
            }
        }
        for index in self._index_names():
            self._delete_by_query_raw(index, query)

    def _delete_generation(self, source_id: str, generation: str) -> None:
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"generation": generation}},
                ]
            }
        }
        for index in self._index_names():
            self._delete_by_query_raw(index, query)

    # -- targeted reads (KnowledgeBackend F4 contract) ---------------------
    def get_files(self, file_ids: list[str]) -> list[FileRecord]:
        files_index = self._index_names()[0]
        out: list[FileRecord] = []
        for batch in _batches(list(file_ids)):
            query = self._filtered({"term": {"doc_kind": "file"}}, {"terms": {"file_id": batch}})
            hits = self._search_raw(files_index, query, len(batch) * 2)
            out.extend(file_record_from_payload(h["_source"]) for h in hits)
        return out

    def list_files(self, source_id: str) -> list[FileRecord]:
        files_index = self._index_names()[0]
        query = self._filtered({"term": {"doc_kind": "file"}}, {"term": {"source_id": source_id}})
        return [file_record_from_payload(p) for p in self._scan(files_index, query, "file_id")]

    def get_entities(self, entity_ids: list[str]) -> list[Entity]:
        content_index = self._index_names()[1]
        out: list[Entity] = []
        for batch in _batches(list(entity_ids)):
            query = self._filtered(
                {"term": {"doc_kind": "entity"}}, {"terms": {"entity_id": batch}}
            )
            hits = self._search_raw(content_index, query, len(batch) * 2)
            out.extend(entity_from_payload(h["_source"]) for h in hits)
        return out

    def list_entities(
        self, *, source_id: str | None = None, query: str | None = None, limit: int = 100
    ) -> list[Entity]:
        content_index = self._index_names()[1]
        clauses: list[dict[str, Any]] = [{"term": {"doc_kind": "entity"}}]
        if source_id:
            clauses.append({"term": {"source_id": source_id}})
        body: dict[str, Any] = self._filtered(*clauses)
        if query:
            body["bool"]["must"] = [
                {
                    "multi_match": {
                        "query": query,
                        "fields": ["name", "qualified_name", "content"],
                    }
                }
            ]
            hits = self._search_raw(content_index, body, limit)
            return [entity_from_payload(h["_source"]) for h in hits]
        return [
            entity_from_payload(p)
            for p in self._scan(content_index, body, "entity_id", limit=limit)
        ]

    def list_source_entities(
        self, source_id: str, *, generation: str | None = None
    ) -> list[Entity]:
        content_index = self._index_names()[1]
        query = self._filtered(
            {"term": {"doc_kind": "entity"}},
            {"term": {"source_id": source_id}},
            generation=generation,
        )
        return [entity_from_payload(p) for p in self._scan(content_index, query, "entity_id")]

    def find_entities_by_names(
        self,
        *,
        names: list[str] | None = None,
        qualified_names: list[str] | None = None,
        source_id: str | None = None,
        generation: str | None = None,
    ) -> list[Entity]:
        content_index = self._index_names()[1]
        out: list[Entity] = []
        for field, values in (("name", names or []), ("qualified_name", qualified_names or [])):
            for batch in _batches(list(values)):
                clauses: list[dict[str, Any]] = [
                    {"term": {"doc_kind": "entity"}},
                    {"terms": {field: batch}},
                ]
                if source_id:
                    clauses.append({"term": {"source_id": source_id}})
                query = self._filtered(*clauses, generation=generation)
                out.extend(
                    entity_from_payload(p) for p in self._scan(content_index, query, "entity_id")
                )
        seen: set[str] = set()
        unique: list[Entity] = []
        for entity in out:
            if entity.id not in seen:
                seen.add(entity.id)
                unique.append(entity)
        return unique

    def get_links(
        self,
        *,
        entity_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[LinkRecord]:
        rel_index = self._index_names()[2]
        out: list[LinkRecord] = []
        seen: set[tuple[str, str, str | None, str, str]] = set()
        for field, values in (("entity_id", entity_ids or []), ("document_id", document_ids or [])):
            for batch in _batches(list(values)):
                query = self._filtered({"term": {"doc_kind": "link"}}, {"terms": {field: batch}})
                for payload in self._scan(rel_index, query, "link_key"):
                    record = link_record_from_payload(payload)
                    key = (
                        record.entity_id,
                        record.document_id,
                        record.section_id,
                        record.link_type,
                        record.resolver,
                    )
                    if key not in seen:
                        seen.add(key)
                        out.append(record)
        return out

    def remove_link(self, link_id: str) -> bool:
        """Deletes one link by its ``LinkRecord.id`` (the stored
        ``link_key`` -- see ``link_record_from_payload``). Server-aware
        ``ragmonk link remove``: unlike ``clear_source``, this must never
        touch any other link, so it filters on the exact ``link_key``
        rather than a broader source/entity/document match.
        """
        rel_index = self._index_names()[2]
        query = self._filtered({"term": {"doc_kind": "link"}}, {"term": {"link_key": link_id}})
        existing = self._scan(rel_index, query, "link_key")
        if not existing:
            return False
        self._delete_by_query_raw(rel_index, query)
        return True

    def get_documents(self, document_ids: list[str]) -> list[DocumentRecord]:
        content_index = self._index_names()[1]
        out: list[DocumentRecord] = []
        for batch in _batches(list(document_ids)):
            query = self._filtered(
                {"term": {"doc_kind": "document"}}, {"terms": {"document_id": batch}}
            )
            hits = self._search_raw(content_index, query, len(batch) * 2)
            out.extend(document_record_from_payload(h["_source"]) for h in hits)
        return out

    def list_documents(
        self, *, source_id: str | None = None, limit: int | None = None
    ) -> list[DocumentRecord]:
        content_index = self._index_names()[1]
        clauses: list[dict[str, Any]] = [{"term": {"doc_kind": "document"}}]
        if source_id:
            clauses.append({"term": {"source_id": source_id}})
        query = self._filtered(*clauses)
        return [
            document_record_from_payload(p)
            for p in self._scan(content_index, query, "document_id", limit=limit)
        ]

    def get_document_units(
        self,
        *,
        document_id: str | None = None,
        unit_ids: list[str] | None = None,
    ) -> list[DocumentUnitRecord]:
        content_index = self._index_names()[1]
        if document_id is not None:
            query = self._filtered(
                {"term": {"doc_kind": "chunk"}}, {"term": {"document_id": document_id}}
            )
            return [
                unit_record_from_payload(p) for p in self._scan(content_index, query, "chunk_id")
            ]
        out: list[DocumentUnitRecord] = []
        for batch in _batches(list(unit_ids or [])):
            query = self._filtered({"term": {"doc_kind": "chunk"}}, {"terms": {"chunk_id": batch}})
            hits = self._search_raw(content_index, query, len(batch) * 2)
            out.extend(unit_record_from_payload(h["_source"]) for h in hits)
        return out

    def list_source_document_units(
        self, source_id: str, *, generation: str | None = None
    ) -> list[DocumentUnitRecord]:
        content_index = self._index_names()[1]
        query = self._filtered(
            {"term": {"doc_kind": "chunk"}},
            {"term": {"source_id": source_id}},
            generation=generation,
        )
        return [unit_record_from_payload(p) for p in self._scan(content_index, query, "chunk_id")]

    def find_relationships_by_target_prefix(
        self, source_id: str, prefix: str, *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        rel_index = self._index_names()[2]
        query = self._filtered(
            {"term": {"doc_kind": "relationship"}},
            {"term": {"source_id": source_id}},
            {"prefix": {"target_symbol": prefix}},
            generation=generation,
        )
        return self._scan(rel_index, query, "relationship_id")

    def find_unresolved_relationships(
        self,
        symbols: list[str],
        relationship_types: list[str] | None = None,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        names = sorted({s for s in symbols if s})
        if not names:
            return []
        rel_index = self._index_names()[2]
        clauses: list[dict[str, Any]] = [
            {"term": {"doc_kind": "relationship"}},
            {"terms": {"target_symbol": names}},
            {"bool": {"must_not": [{"exists": {"field": "target_entity_id"}}]}},
        ]
        if relationship_types:
            clauses.append({"terms": {"relationship_type": list(relationship_types)}})
        query = self._filtered(*clauses)
        return self._scan(rel_index, query, "relationship_id", limit=limit)

    # -- write helpers shared by both adapters ------------------------------
    def _entity_file_ids(self, entity_ids: list[str]) -> dict[str, str]:
        """entity_id -> file_id, across *every* generation (a link is
        written in the same pass -- and generation -- as the entity it
        references, which may not be published yet).
        """
        content_index = self._index_names()[1]
        out: dict[str, str] = {}
        for batch in _batches(entity_ids):
            query = {
                "bool": {
                    "filter": [{"term": {"doc_kind": "entity"}}, {"terms": {"entity_id": batch}}]
                }
            }
            for hit in self._search_raw(content_index, query, len(batch) * 4):
                src = hit.get("_source", {})
                out[str(src.get("entity_id"))] = str(src.get("file_id", ""))
        return out

    def _document_file_ids(self, document_ids: list[str]) -> dict[str, str]:
        content_index = self._index_names()[1]
        out: dict[str, str] = {}
        for batch in _batches(document_ids):
            query = {
                "bool": {
                    "filter": [
                        {"term": {"doc_kind": "document"}},
                        {"terms": {"document_id": batch}},
                    ]
                }
            }
            for hit in self._search_raw(content_index, query, len(batch) * 4):
                src = hit.get("_source", {})
                out[str(src.get("document_id"))] = str(src.get("file_id", ""))
        return out
