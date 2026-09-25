"""``ElasticsearchKnowledgeBackend`` -- a ``KnowledgeBackend`` adapter for
a server-mode Elasticsearch cluster.

Storage backend abstraction plan, Phase 5. ``elasticsearch`` (the
official Python client) stays an OPTIONAL dependency: nothing at this
module's top level imports it -- only
``elasticsearch_client.build_client`` does, lazily, inside a function
body -- so ``import ragmonk.backends.elasticsearch`` and constructing a
*local*-mode backend via ``ragmonk.backends.factory.create_backend``
never requires it to be installed. Constructing *this* class does need
it (raises
:class:`~ragmonk.backends.elasticsearch_client.ElasticsearchDependencyError`
with a clear install hint if it's missing).

Index design (see ``elasticsearch_mappings.py`` for the exact mappings):
``{prefix}-files``, ``{prefix}-content``, ``{prefix}-relationships`` --
the same three-index-per-prefix shape as the OpenSearch adapter for
backend-contract consistency. Every write goes through the bounded bulk
helper in ``elasticsearch_bulk.py`` -- never a per-entity/per-chunk HTTP
request -- using deterministic ids from ``elasticsearch_ids.py`` so
re-publishing the same file is an idempotent overwrite, not a duplicate.

Generation (rebuild-safety) design: exactly the same generation-marker-
document approach as the OpenSearch adapter (see that module's docstring
for the full rationale -- avoiding one physical index per generation).
Every content/relationship document is tagged with a ``generation``
keyword field, and each source's *currently active* generation is
recorded in one small "generation marker" document in the files index
(id derived by ``elasticsearch_ids.generation_marker_id``). Every read
method that cares about generation-correctness resolves that marker
first; ``abort_generation`` deletes the incomplete generation's documents
(``delete_by_query``) without touching the marker.

This module and ``opensearch.py`` are deliberately NOT shared beyond the
backend-neutral models/config -- Elasticsearch's client library, bulk
response shape (structurally similar but a genuinely separate library),
and vector-field mapping syntax (native ``dense_vector``/``knn`` vs.
OpenSearch's k-NN-plugin ``knn_vector``) are treated as separate
concerns per this project's stated architecture principle.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ragmonk.backends import elasticsearch_ids as ids
from ragmonk.backends import elasticsearch_mappings as mappings
from ragmonk.backends.base import GraphDirection, KnowledgeBackend
from ragmonk.backends.elasticsearch_bulk import BulkAction, run_bulk_or_raise
from ragmonk.backends.elasticsearch_client import (
    ElasticsearchConnectionError,
    build_client,
    cluster_health,
)
from ragmonk.backends.factory import credential_env_vars
from ragmonk.backends.models import (
    BackendStats,
    FileRecord,
    PreparedCode,
    PreparedDocument,
    PreparedEmbeddings,
    PreparedLinks,
    SearchHit,
)
from ragmonk.core.config import ServerStorageConfig
from ragmonk.core.models import EmbeddingSubjectType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from elasticsearch import Elasticsearch

_DEFAULT_ACTIVE_GENERATION = "0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ElasticsearchKnowledgeBackend(KnowledgeBackend):
    """A ``KnowledgeBackend`` backed by a real Elasticsearch cluster.

    Never falls back to local storage: every method either performs a
    real Elasticsearch call or raises. A cluster that is unreachable,
    misconfigured, or running an unsupported version surfaces as an
    :class:`ElasticsearchConnectionError`/
    :class:`~ragmonk.backends.elasticsearch_client.ElasticsearchVersionError`
    (both :class:`~ragmonk.core.errors.HealthCheckError`) or
    :class:`~ragmonk.backends.elasticsearch_bulk.BulkIndexError` (a
    :class:`~ragmonk.core.errors.DatabaseError`) -- all typed, all
    carrying no credential value -- never an empty/partial result that
    could be mistaken for "nothing found".
    """

    def __init__(
        self, config: ServerStorageConfig, *, client: Elasticsearch | None = None
    ) -> None:
        self._config = config
        self._prefix = config.index_prefix
        self._client_override = client
        self._client: Elasticsearch | None = client
        self._vector_dims: int | None = None

    # -- client -----------------------------------------------------------
    def _get_client(self) -> Elasticsearch:
        if self._client is None:
            username_var, password_var, api_key_var = credential_env_vars("elasticsearch")
            self._client = build_client(
                url=self._config.url,
                verify_tls=self._config.verify_tls,
                request_timeout_seconds=self._config.request_timeout_seconds,
                username_var=username_var,
                password_var=password_var,
                api_key_var=api_key_var,
            )
        return self._client

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        """Returns True if reachable and running a supported version;
        False only for a graceful "cannot reach"/"unsupported version"
        condition -- ``cluster_health`` itself still raises for callers
        wanting the engine name/version (rather than a bool), e.g. via
        :meth:`describe`.
        """
        try:
            self.describe()
            return True
        except ElasticsearchConnectionError:
            return False

    def describe(self) -> tuple[str, str]:
        """Returns ``(engine_name, version)`` from a real cluster call.
        Raises :class:`ElasticsearchConnectionError` on failure, or
        :class:`~ragmonk.backends.elasticsearch_client.ElasticsearchVersionError`
        for an unsupported (pre-8.0) cluster version.
        """
        return cluster_health(self._get_client())

    def ensure_schema(self) -> None:
        mappings.ensure_schema(self._get_client(), self._prefix)

    def close(self) -> None:
        client = self._client
        if client is not None and self._client_override is None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._client = None

    # -- generation lifecycle -------------------------------------------
    def _active_generation(self, source_id: str) -> str:
        client = self._get_client()
        try:
            doc = client.get(
                index=mappings.files_index(self._prefix),
                id=ids.generation_marker_id(source_id),
            )
        except Exception:
            return _DEFAULT_ACTIVE_GENERATION
        doc_dict = dict(doc) if not isinstance(doc, dict) else doc
        source = doc_dict.get("_source", {})
        return str(source.get("active_generation", _DEFAULT_ACTIVE_GENERATION))

    def begin_generation(self, source_id: str) -> str:
        current = self._active_generation(source_id)
        try:
            return str(int(current) + 1)
        except ValueError:
            return uuid.uuid4().hex

    def publish_generation(self, source_id: str, generation: str) -> None:
        client = self._get_client()
        client.index(
            index=mappings.files_index(self._prefix),
            id=ids.generation_marker_id(source_id),
            document={
                "doc_kind": "generation_marker",
                "source_id": source_id,
                "active_generation": generation,
                "updated_at": _now(),
            },
        )
        for index in mappings.all_indices(self._prefix):
            client.indices.refresh(index=index)

    def abort_generation(self, source_id: str, generation: str) -> None:
        client = self._get_client()
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"generation": generation}},
                ]
            }
        }
        for index in (
            mappings.content_index(self._prefix),
            mappings.relationships_index(self._prefix),
        ):
            client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")

    # -- writes -----------------------------------------------------------
    def upsert_file(self, file_record: FileRecord) -> None:
        action = BulkAction(
            op="index",
            index=mappings.files_index(self._prefix),
            doc_id=ids.file_doc_id(file_record.source_id, file_record.file_id),
            source={
                "doc_kind": "file",
                "source_id": file_record.source_id,
                "file_id": file_record.file_id,
                "path": file_record.path,
                "content_hash": file_record.content_hash,
                "size_bytes": file_record.size_bytes,
                "mtime": file_record.mtime,
                "metadata": file_record.metadata,
            },
        )
        run_bulk_or_raise(self._get_client(), [action], self._config.bulk)
        self._get_client().indices.refresh(index=mappings.files_index(self._prefix))

    def delete_file(self, source_id: str, file_id: str) -> None:
        client = self._get_client()
        try:
            client.delete(
                index=mappings.files_index(self._prefix),
                id=ids.file_doc_id(source_id, file_id),
            )
        except Exception as exc:
            # The `elasticsearch` client raises a typed NotFoundError for a
            # missing document (there is no `ignore=[404]` kwarg on
            # `delete`, unlike opensearch-py) -- deleting an already-
            # absent file is a no-op, not an error; any other failure
            # (connection error, auth, etc.) still propagates.
            if type(exc).__name__ != "NotFoundError":
                raise
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"file_id": file_id}},
                ]
            }
        }
        for index in (
            mappings.content_index(self._prefix),
            mappings.relationships_index(self._prefix),
        ):
            client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")
        client.indices.refresh(index=mappings.files_index(self._prefix))

    def publish_code(self, prepared_code: PreparedCode) -> None:
        """Deletes this file's previous-generation entities/relationships
        (matching ``LocalKnowledgeBackend.publish_code``'s delete-then-
        insert semantics) and bulk-indexes the new ones, all through the
        bounded bulk path.
        """
        source_id = prepared_code.source_id
        file_id = prepared_code.file_id
        self._delete_file_scoped(mappings.content_index(self._prefix), source_id, file_id, "entity")
        self._delete_file_scoped(
            mappings.relationships_index(self._prefix), source_id, file_id, "relationship"
        )
        if prepared_code.clear_only:
            return

        generation = str(prepared_code.generation)
        actions: list[BulkAction] = []
        for entity in prepared_code.entities:
            snippet = prepared_code.entity_snippets.get(entity.id, entity.name)
            actions.append(
                BulkAction(
                    op="index",
                    index=mappings.content_index(self._prefix),
                    doc_id=ids.entity_doc_id(source_id, file_id, entity.id),
                    source={
                        "doc_kind": "entity",
                        "source_id": source_id,
                        "file_id": file_id,
                        "generation": generation,
                        "entity_id": entity.id,
                        "kind": str(entity.kind),
                        "name": entity.name,
                        "qualified_name": entity.qualified_name,
                        "language": entity.language,
                        "content": snippet,
                        "snippet": snippet,
                        "start_line": entity.start_line,
                        "end_line": entity.end_line,
                        "created_at": entity.created_at,
                        "updated_at": entity.updated_at,
                    },
                )
            )
        for relationship in prepared_code.relationships:
            actions.append(
                BulkAction(
                    op="index",
                    index=mappings.relationships_index(self._prefix),
                    doc_id=ids.relationship_doc_id(source_id, file_id, relationship.id),
                    source={
                        "doc_kind": "relationship",
                        "source_id": source_id,
                        "file_id": file_id,
                        "generation": generation,
                        "relationship_type": str(relationship.relationship_type),
                        "source_entity_id": relationship.source_entity_id,
                        "target_entity_id": relationship.target_entity_id,
                        "target_symbol": relationship.target_symbol,
                        "resolver": relationship.resolver,
                        "confidence": str(relationship.confidence),
                        "evidence": relationship.evidence,
                        "created_at": relationship.created_at,
                    },
                )
            )
        if actions:
            run_bulk_or_raise(self._get_client(), actions, self._config.bulk)
            self._refresh_content_and_relationships()

    def publish_document(self, prepared_document: PreparedDocument) -> None:
        source_id = prepared_document.source_id
        file_id = prepared_document.file_id
        content_index = mappings.content_index(self._prefix)
        self._delete_file_scoped(content_index, source_id, file_id, "document")
        self._delete_file_scoped(content_index, source_id, file_id, "chunk")
        if prepared_document.delete_only or prepared_document.document is None:
            return

        generation = str(prepared_document.generation)
        document = prepared_document.document
        doc_title = prepared_document.doc_title
        actions = [
            BulkAction(
                op="index",
                index=mappings.content_index(self._prefix),
                doc_id=ids.document_doc_id(source_id, file_id),
                source={
                    "doc_kind": "document",
                    "source_id": source_id,
                    "file_id": file_id,
                    "generation": generation,
                    "document_id": document.id,
                    "kind": str(document.format),
                    "name": doc_title,
                    "content": doc_title,
                    "search_text": doc_title,
                    "created_at": document.created_at,
                    "updated_at": document.updated_at,
                },
            )
        ]
        for chunk_id, chunk in zip(
            prepared_document.chunk_ids, prepared_document.chunks, strict=True
        ):
            actions.append(
                BulkAction(
                    op="index",
                    index=mappings.content_index(self._prefix),
                    doc_id=ids.chunk_doc_id(source_id, file_id, chunk_id),
                    source={
                        "doc_kind": "chunk",
                        "source_id": source_id,
                        "file_id": file_id,
                        "generation": generation,
                        "chunk_id": chunk_id,
                        "document_id": document.id,
                        "kind": chunk.kind,
                        "heading_path": list(chunk.heading_path),
                        "content": chunk.text,
                        "search_text": chunk.search_text,
                        "created_at": document.created_at,
                        "updated_at": document.updated_at,
                    },
                )
            )
        run_bulk_or_raise(self._get_client(), actions, self._config.bulk)
        self._get_client().indices.refresh(index=mappings.content_index(self._prefix))

    def publish_embeddings(self, prepared_embeddings: PreparedEmbeddings) -> None:
        """Updates each already-published entity/chunk document's
        ``embedding`` field in place -- embeddings never create new
        content documents, they augment the entity/document-chunk
        document ``publish_code``/``publish_document`` already wrote.
        """
        if not prepared_embeddings.subjects:
            return

        dims = len(prepared_embeddings.vectors[0]) if prepared_embeddings.vectors else 0
        if dims and dims != self._vector_dims:
            mappings.ensure_vector_field(self._get_client(), self._prefix, dims)
            self._vector_dims = dims

        actions: list[BulkAction] = []
        for (subject_type, subject_id, file_id, _text), vector in zip(
            prepared_embeddings.subjects, prepared_embeddings.vectors, strict=True
        ):
            source_id = prepared_embeddings.source_id
            doc_id = (
                ids.entity_doc_id(source_id, file_id, subject_id)
                if subject_type == EmbeddingSubjectType.ENTITY
                else ids.chunk_doc_id(source_id, file_id, subject_id)
            )
            actions.append(
                BulkAction(
                    op="update",
                    index=mappings.content_index(self._prefix),
                    doc_id=doc_id,
                    source={"embedding": vector},
                )
            )
        # `update` (partial-doc merge), not `index`: these layer an
        # `embedding` field onto an existing content document
        # (`publish_code`/`publish_document` already wrote it) -- a plain
        # `index` action with only the changed field would wipe the rest.
        run_bulk_or_raise(self._get_client(), actions, self._config.bulk)
        self._get_client().indices.refresh(index=mappings.content_index(self._prefix))

    def publish_links(self, prepared_links: PreparedLinks) -> int:
        if not prepared_links.candidates:
            return 0
        source_id = prepared_links.source_id
        actions = [
            BulkAction(
                op="index",
                index=mappings.relationships_index(self._prefix),
                doc_id=ids.link_doc_id(
                    source_id,
                    candidate.entity_id,
                    candidate.document_id,
                    candidate.section_id,
                    str(candidate.link_type),
                    candidate.resolver,
                ),
                source={
                    "doc_kind": "link",
                    "source_id": source_id,
                    "relationship_type": str(candidate.link_type),
                    "entity_id": candidate.entity_id,
                    "document_id": candidate.document_id,
                    "section_id": candidate.section_id,
                    "resolver": candidate.resolver,
                    "confidence": str(candidate.confidence),
                    "evidence": candidate.evidence,
                    "created_at": _now(),
                },
            )
            for candidate in prepared_links.candidates
        ]
        # Deterministic ids make this idempotent -- inserting the same
        # candidate twice overwrites the same document rather than
        # duplicating, so "newly inserted" is exactly the count of ids
        # not already present before this call.
        client = self._get_client()
        existing = {
            action.doc_id
            for action in actions
            if client.exists(index=mappings.relationships_index(self._prefix), id=action.doc_id)
        }
        run_bulk_or_raise(client, actions, self._config.bulk)
        client.indices.refresh(index=mappings.relationships_index(self._prefix))
        return len(actions) - len(existing)

    def clear_source(self, source_id: str) -> None:
        """Deletes every document belonging to ``source_id`` across all
        three indices -- files, content, and relationships -- so no stale
        record for that source survives anywhere.
        """
        client = self._get_client()
        query = {"term": {"source_id": source_id}}
        for index in mappings.all_indices(self._prefix):
            client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")

    # -- internal helpers ---------------------------------------------------
    def _delete_file_scoped(self, index: str, source_id: str, file_id: str, doc_kind: str) -> None:
        client = self._get_client()
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"file_id": file_id}},
                    {"term": {"doc_kind": doc_kind}},
                ]
            }
        }
        client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")

    def _refresh_content_and_relationships(self) -> None:
        client = self._get_client()
        client.indices.refresh(index=mappings.content_index(self._prefix))
        client.indices.refresh(index=mappings.relationships_index(self._prefix))

    # -- reads / search ---------------------------------------------------
    def lexical_search(
        self, query: str, limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        must = [
            {"multi_match": {"query": query, "fields": ["content^2", "search_text", "name"]}}
        ]
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=limit,
            query={"bool": {"must": must, "filter": self._filter_clauses(filters)}},
        )
        return _hits_to_search_hits(response)

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        """Native Elasticsearch ``knn`` query against the ``embedding``
        ``dense_vector`` field -- distinct from OpenSearch's k-NN-plugin
        query DSL (a top-level ``knn`` search parameter here, not a
        ``knn`` bool-query clause).
        """
        client = self._get_client()
        knn = {
            "field": "embedding",
            "query_vector": vector,
            "k": limit,
            "num_candidates": max(limit * 10, limit),
        }
        filter_clauses = self._filter_clauses(filters)
        if filter_clauses:
            knn["filter"] = {"bool": {"filter": filter_clauses}}
        response = client.search(
            index=mappings.content_index(self._prefix),
            size=limit,
            knn=knn,
        )
        return _hits_to_search_hits(response)

    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        must = [
            {"term": {"doc_kind": "entity"}},
            {"bool": {"should": [{"term": {"name": name}}, {"term": {"qualified_name": name}}]}},
        ]
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=50,
            query={"bool": {"must": must, "filter": self._filter_clauses(filters)}},
        )
        return _hits_to_search_hits(response)

    def graph_neighbors(
        self,
        entity_id: str,
        direction: GraphDirection,
        depth: int,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        client = self._get_client()
        index = mappings.relationships_index(self._prefix)
        seen: set[str] = {entity_id}
        frontier = {entity_id}
        results: list[SearchHit] = []
        for _ in range(max(depth, 1)):
            if not frontier:
                break
            should = []
            for eid in frontier:
                if direction in ("out", "both"):
                    should.append({"term": {"source_entity_id": eid}})
                if direction in ("in", "both"):
                    should.append({"term": {"target_entity_id": eid}})
            if not should:
                break
            response = client.search(
                index=index,
                size=500,
                query={
                    "bool": {
                        "should": should,
                        "minimum_should_match": 1,
                        "filter": self._filter_clauses(filters),
                    }
                },
            )
            hits = _hits_to_search_hits(response)
            next_frontier: set[str] = set()
            for hit in hits:
                if hit.id not in seen:
                    seen.add(hit.id)
                    results.append(hit)
                target = hit.payload.get("target_entity_id")
                source = hit.payload.get("source_entity_id")
                for candidate in (target, source):
                    if candidate and candidate not in seen:
                        next_frontier.add(candidate)
            frontier = next_frontier
        return results

    def get_file(self, file_id: str) -> FileRecord | None:
        response = self._get_client().search(
            index=mappings.files_index(self._prefix),
            size=1,
            query={"term": {"file_id": file_id}},
        )
        response_dict = dict(response) if not isinstance(response, dict) else response
        hits = response_dict.get("hits", {}).get("hits", [])
        if not hits:
            return None
        source = hits[0]["_source"]
        return FileRecord(
            file_id=source["file_id"],
            source_id=source["source_id"],
            path=source.get("path", ""),
            content_hash=source.get("content_hash", ""),
            size_bytes=source.get("size_bytes", 0),
            mtime=source.get("mtime"),
            metadata=source.get("metadata") or {},
        )

    def get_entities_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=10_000,
            query={
                "bool": {
                    "filter": [
                        {"term": {"doc_kind": "entity"}},
                        {"terms": {"file_id": file_ids}},
                    ]
                }
            },
        )
        response_dict = dict(response) if not isinstance(response, dict) else response
        return [hit["_source"] for hit in response_dict.get("hits", {}).get("hits", [])]

    def get_document_units_for_files(self, file_ids: list[str]) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=10_000,
            query={
                "bool": {
                    "filter": [
                        {"term": {"doc_kind": "chunk"}},
                        {"terms": {"file_id": file_ids}},
                    ]
                }
            },
        )
        response_dict = dict(response) if not isinstance(response, dict) else response
        return [hit["_source"] for hit in response_dict.get("hits", {}).get("hits", [])]

    def count_stats(self) -> BackendStats:
        client = self._get_client()
        files_count = _count(
            client, mappings.files_index(self._prefix), {"term": {"doc_kind": "file"}}
        )
        content_index = mappings.content_index(self._prefix)
        entities_count = _count(client, content_index, {"term": {"doc_kind": "entity"}})
        document_units_count = _count(client, content_index, {"term": {"doc_kind": "chunk"}})
        embeddings_count = _count(client, content_index, {"exists": {"field": "embedding"}})
        return BackendStats(
            files=files_count,
            entities=entities_count,
            document_units=document_units_count,
            embeddings=embeddings_count,
        )

    def _filter_clauses(self, filters: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not filters:
            return []
        clauses: list[dict[str, Any]] = []
        for key, value in filters.items():
            if isinstance(value, list | tuple | set):
                clauses.append({"terms": {key: list(value)}})
            else:
                clauses.append({"term": {key: value}})
        return clauses


def _count(client: Any, index: str, query: dict[str, Any]) -> int:
    response = client.count(index=index, query=query)
    response_dict = dict(response) if not isinstance(response, dict) else response
    return int(response_dict["count"])


def _hits_to_search_hits(response: Any) -> list[SearchHit]:
    response_dict = dict(response) if not isinstance(response, dict) else response
    hits = response_dict.get("hits", {}).get("hits", [])
    results = []
    for hit in hits:
        source = hit.get("_source", {})
        results.append(
            SearchHit(
                id=str(hit.get("_id")),
                score=float(hit.get("_score") or 0.0),
                kind=str(source.get("doc_kind", "document")),
                payload=source,
            )
        )
    return results
