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
Every content/relationship document written by ``publish_code``/
``publish_document`` is tagged with a ``generation`` keyword field, and
each source's *currently active* generation is recorded in one small
"generation marker" document in the files index (id derived by
``elasticsearch_ids.generation_marker_id``, a single by-id ``get``, not
a search). Every read method that queries the content or relationships
index (``lexical_search``/``semantic_search``/``symbol_search``/
``graph_neighbors``/``get_entities_for_files``/
``get_document_units_for_files``/``count_stats``) builds a filter clause
via ``_active_generations_map``/``_generation_filter_clause`` that
constrains matches to each source's currently-published generation, so
an in-progress (not yet published) generation's documents are never
returned by any read. File records and cross-domain links are
generation-tagged too, publishing garbage-collects older generations and
aborting removes every artifact of the aborted generation (completion
plan F6 -- see ``opensearch.py``'s docstring for the full design).
``clear_source`` is deliberately NOT generation-filtered -- a full-source
delete must remove every generation, not just the active one.

This module and ``opensearch.py`` are deliberately NOT shared beyond the
backend-neutral models/config and ``server_common.ServerReadMixin`` (the
engine-neutral targeted reads, built on a per-engine ``_search_raw``
hook) -- Elasticsearch's client library, bulk
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
from ragmonk.backends.server_common import ServerReadMixin
from ragmonk.core.config import ServerStorageConfig
from ragmonk.core.models import EmbeddingSubjectType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from elasticsearch import Elasticsearch

_DEFAULT_ACTIVE_GENERATION = "0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ElasticsearchKnowledgeBackend(ServerReadMixin, KnowledgeBackend):
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

    # -- ServerReadMixin hooks (completion plan F4/F6) -----------------------
    @property
    def is_server(self) -> bool:
        return True

    def _index_names(self) -> tuple[str, str, str]:
        return (
            mappings.files_index(self._prefix),
            mappings.content_index(self._prefix),
            mappings.relationships_index(self._prefix),
        )

    def _search_raw(
        self,
        index: str,
        query: dict[str, Any],
        size: int,
        sort: list[dict[str, Any]] | None = None,
        search_after: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"index": index, "size": size, "query": query}
        if sort is not None:
            kwargs["sort"] = sort
        if search_after is not None:
            kwargs["search_after"] = search_after
        response = self._get_client().search(**kwargs)
        response_dict = dict(response) if not isinstance(response, dict) else response
        hits: list[dict[str, Any]] = response_dict.get("hits", {}).get("hits", [])
        return hits

    def _delete_by_query_raw(self, index: str, query: dict[str, Any]) -> None:
        self._get_client().delete_by_query(
            index=index, query=query, refresh=True, conflicts="proceed"
        )

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

    def index_status(self) -> dict[str, bool]:
        """Whether each expected index (files/content/relationships) that
        ``ensure_schema`` would create already exists -- a lightweight
        ``doctor``-only existence check, not a full mapping validation.
        Storage backend abstraction plan, Phase 8.
        """
        client = self._get_client()
        names = (
            mappings.files_index(self._prefix),
            mappings.content_index(self._prefix),
            mappings.relationships_index(self._prefix),
        )
        return {name: bool(client.indices.exists(index=name)) for name in names}

    def close(self) -> None:
        client = self._client
        if client is not None and self._client_override is None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        self._client = None

    # -- generation lifecycle -------------------------------------------
    def _generation_marker_source(self, source_id: str) -> dict[str, Any]:
        client = self._get_client()
        try:
            doc = client.get(
                index=mappings.files_index(self._prefix),
                id=ids.generation_marker_id(source_id),
            )
        except Exception:
            return {}
        doc_dict = dict(doc) if not isinstance(doc, dict) else doc
        return doc_dict.get("_source", {})

    def _active_generation(self, source_id: str) -> str:
        source = self._generation_marker_source(source_id)
        return str(source.get("active_generation", _DEFAULT_ACTIVE_GENERATION))

    def _active_generations_map(self) -> dict[str, str]:
        """Every source's currently-active generation, keyed by
        ``source_id``. A single bounded search over the (small) set of
        generation-marker documents in the files index -- not one call
        per source -- so a read method that isn't pre-scoped to a
        specific source can resolve every relevant source's active
        generation in one round trip.
        """
        client = self._get_client()
        try:
            response = client.search(
                index=mappings.files_index(self._prefix),
                size=10_000,
                query={"term": {"doc_kind": "generation_marker"}},
            )
        except Exception:
            return {}
        response_dict = dict(response) if not isinstance(response, dict) else response
        hits = response_dict.get("hits", {}).get("hits", [])
        result: dict[str, str] = {}
        for hit in hits:
            source = hit.get("_source", {})
            source_id = source.get("source_id")
            if source_id:
                result[str(source_id)] = str(
                    source.get("active_generation", _DEFAULT_ACTIVE_GENERATION)
                )
        return result

    def _generation_filter_clause(self) -> dict[str, Any]:
        """A query clause that constrains matches to each source's
        currently-published generation -- same semantics as the
        OpenSearch adapter's ``_generation_filter_clause`` (see that
        module's docstring): known sources match on
        ``(source_id, generation)``, an unmarked source falls back to
        ``_DEFAULT_ACTIVE_GENERATION``, and a document with no
        ``generation`` field at all (a cross-domain link) always
        matches.
        """
        gen_map = self._active_generations_map()
        should: list[dict[str, Any]] = [
            {"bool": {"filter": [{"term": {"source_id": sid}}, {"term": {"generation": gen}}]}}
            for sid, gen in gen_map.items()
        ]
        default_clause: dict[str, Any] = {
            "bool": {"filter": [{"term": {"generation": _DEFAULT_ACTIVE_GENERATION}}]}
        }
        if gen_map:
            default_clause["bool"]["must_not"] = [{"terms": {"source_id": list(gen_map)}}]
        should.append(default_clause)
        should.append({"bool": {"must_not": [{"exists": {"field": "generation"}}]}})
        return {"bool": {"should": should, "minimum_should_match": 1}}

    def begin_generation(self, source_id: str) -> str:
        """Issues a new generation id, guaranteed never to repeat a value
        this method has already handed out for ``source_id`` -- even
        across a failed rebuild whose ``abort_generation`` never ran or
        itself failed (independent review follow-up: a retry must not
        reuse a generation number that may still have uncleaned documents
        tagged with it -- see ``OpenSearchKnowledgeBackend.begin_generation``'s
        docstring for the full reasoning, mirrored here).

        The marker document persists not just the *published*
        ``active_generation`` but also the highest generation ever
        *begun* (``last_begun_generation``), written eagerly by this
        method itself -- before any content is touched, let alone
        published. The next id is always one past
        ``max(active_generation, last_begun_generation)``.
        """
        marker = self._generation_marker_source(source_id)
        active = str(marker.get("active_generation", _DEFAULT_ACTIVE_GENERATION))
        last_begun = str(marker.get("last_begun_generation", active))
        try:
            new_generation = str(max(int(active), int(last_begun)) + 1)
        except ValueError:
            new_generation = uuid.uuid4().hex
        client = self._get_client()
        client.index(
            index=mappings.files_index(self._prefix),
            id=ids.generation_marker_id(source_id),
            document={
                "doc_kind": "generation_marker",
                "source_id": source_id,
                "active_generation": active,
                "last_begun_generation": new_generation,
                "updated_at": _now(),
            },
        )
        return new_generation

    def publish_generation(self, source_id: str, generation: str) -> None:
        client = self._get_client()
        client.index(
            index=mappings.files_index(self._prefix),
            id=ids.generation_marker_id(source_id),
            document={
                "doc_kind": "generation_marker",
                "source_id": source_id,
                "active_generation": generation,
                "last_begun_generation": generation,
                "updated_at": _now(),
            },
        )
        for index in mappings.all_indices(self._prefix):
            client.indices.refresh(index=index)
        # Completion plan F6: see OpenSearchKnowledgeBackend.publish_generation.
        self._gc_other_generations(source_id, generation)

    def abort_generation(self, source_id: str, generation: str) -> None:
        """Deletes every artifact of the aborted generation in every index
        (file records and links included -- completion plan F6); the
        marker, and so the published generation, is untouched.
        """
        self._delete_generation(source_id, generation)

    # -- writes -----------------------------------------------------------
    def upsert_file(self, file_record: FileRecord) -> None:
        self.upsert_files([file_record])

    def upsert_files(self, file_records: list[FileRecord]) -> None:
        """Generation-tagged file records (completion plan F6) -- see
        ``OpenSearchKnowledgeBackend.upsert_files``.
        """
        if not file_records:
            return
        generations: dict[str, str] = {}
        actions: list[BulkAction] = []
        for record in file_records:
            key = f"{record.source_id}\x1f{record.generation}"
            if key not in generations:
                generations[key] = self._write_generation(record.source_id, record.generation)
            generation = generations[key]
            actions.append(
                BulkAction(
                    op="index",
                    index=mappings.files_index(self._prefix),
                    doc_id=ids.file_doc_id(record.source_id, record.file_id, generation),
                    source={
                        "doc_kind": "file",
                        "source_id": record.source_id,
                        "file_id": record.file_id,
                        "generation": generation,
                        "path": record.path,
                        "content_hash": record.content_hash,
                        "size_bytes": record.size_bytes,
                        "mtime": record.mtime,
                        "metadata": record.metadata,
                    },
                )
            )
        run_bulk_or_raise(self._get_client(), actions, self._config.bulk)
        self._get_client().indices.refresh(index=mappings.files_index(self._prefix))

    def delete_file(self, source_id: str, file_id: str) -> None:
        """Removes every artifact of ``file_id`` across all generations,
        including cross-domain links touching it -- see
        ``OpenSearchKnowledgeBackend.delete_file``.
        """
        client = self._get_client()
        scoped = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"file_id": file_id}},
                ]
            }
        }
        for index in mappings.all_indices(self._prefix):
            client.delete_by_query(index=index, query=scoped, refresh=True, conflicts="proceed")
        links = {
            "bool": {
                "filter": [{"term": {"source_id": source_id}}, {"term": {"doc_kind": "link"}}],
                "should": [
                    {"term": {"entity_file_id": file_id}},
                    {"term": {"document_file_id": file_id}},
                ],
                "minimum_should_match": 1,
            }
        }
        client.delete_by_query(
            index=mappings.relationships_index(self._prefix),
            query=links,
            refresh=True,
            conflicts="proceed",
        )

    def publish_code(self, prepared_code: PreparedCode) -> None:
        """Deletes this file's previous-generation entities/relationships
        (matching ``LocalKnowledgeBackend.publish_code``'s delete-then-
        insert semantics) and bulk-indexes the new ones, all through the
        bounded bulk path.
        """
        source_id = prepared_code.source_id
        file_id = prepared_code.file_id
        # Completion plan F6: delete only the generation being written.
        generation = str(prepared_code.generation)
        self._delete_file_scoped(
            mappings.content_index(self._prefix), source_id, file_id, "entity", generation
        )
        self._delete_file_scoped(
            mappings.relationships_index(self._prefix),
            source_id,
            file_id,
            "relationship",
            generation,
        )
        self._delete_links_for_file(source_id, file_id, "entity_file_id", generation)
        if prepared_code.clear_only:
            return

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
                        "signature": entity.signature,
                        "parent_id": entity.parent_id,
                        "start_line": entity.start_line,
                        "end_line": entity.end_line,
                        "start_col": entity.start_col,
                        "end_col": entity.end_col,
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
                        "relationship_id": relationship.id,
                        "relationship_type": str(relationship.relationship_type),
                        "source_entity_id": relationship.source_entity_id,
                        "target_entity_id": relationship.target_entity_id,
                        "target_symbol": relationship.target_symbol,
                        "resolver": relationship.resolver,
                        "confidence": str(relationship.confidence),
                        "evidence": relationship.evidence,
                        "source_location": relationship.source_location,
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
        generation = str(prepared_document.generation)
        self._delete_file_scoped(content_index, source_id, file_id, "document", generation)
        self._delete_file_scoped(content_index, source_id, file_id, "chunk", generation)
        self._delete_links_for_file(source_id, file_id, "document_file_id", generation)
        if prepared_document.delete_only or prepared_document.document is None:
            return

        document = prepared_document.document
        doc_title = prepared_document.doc_title
        actions = [
            BulkAction(
                op="index",
                index=mappings.content_index(self._prefix),
                doc_id=ids.document_doc_id(source_id, file_id, generation),
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
                    "doc_meta": _document_meta(document),
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
                        "embedding_text": chunk.contextual_text,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
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
        generation = self._write_generation(source_id, prepared_links.generation)
        entity_files = self._entity_file_ids([c.entity_id for c in prepared_links.candidates])
        document_files = self._document_file_ids(
            [c.document_id for c in prepared_links.candidates]
        )
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
                    generation,
                ),
                source={
                    "doc_kind": "link",
                    "link_key": ids.link_doc_id(
                        source_id,
                        candidate.entity_id,
                        candidate.document_id,
                        candidate.section_id,
                        str(candidate.link_type),
                        candidate.resolver,
                        generation,
                    ),
                    "source_id": source_id,
                    "generation": generation,
                    "entity_file_id": entity_files.get(candidate.entity_id, ""),
                    "document_file_id": document_files.get(candidate.document_id, ""),
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
        record for that source survives anywhere. Deliberately NOT
        generation-filtered: this is a full-source delete, so it must
        remove every generation's documents (including any in-progress,
        unpublished one), not just the currently active generation's --
        a different operation from the read-time isolation the other
        methods on this class implement.
        """
        client = self._get_client()
        query = {"term": {"source_id": source_id}}
        for index in mappings.all_indices(self._prefix):
            client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")

    # -- internal helpers ---------------------------------------------------
    def _delete_file_scoped(
        self, index: str, source_id: str, file_id: str, doc_kind: str, generation: str
    ) -> None:
        client = self._get_client()
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"file_id": file_id}},
                    {"term": {"doc_kind": doc_kind}},
                    {"term": {"generation": generation}},
                ]
            }
        }
        client.delete_by_query(index=index, query=query, refresh=True, conflicts="proceed")

    def _delete_links_for_file(
        self, source_id: str, file_id: str, field: str, generation: str
    ) -> None:
        query = {
            "bool": {
                "filter": [
                    {"term": {"source_id": source_id}},
                    {"term": {"doc_kind": "link"}},
                    {"term": {field: file_id}},
                    {"term": {"generation": generation}},
                ]
            }
        }
        self._get_client().delete_by_query(
            index=mappings.relationships_index(self._prefix),
            query=query,
            refresh=True,
            conflicts="proceed",
        )

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
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=limit,
            query={"bool": {"must": must, "filter": filter_clauses}},
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
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
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
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
        response = self._get_client().search(
            index=mappings.content_index(self._prefix),
            size=50,
            query={"bool": {"must": must, "filter": filter_clauses}},
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
        # Resolved once per call, not once per depth: the active-generation
        # map is the same across all depths of a single traversal.
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
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
                        "filter": filter_clauses,
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
        """Generation-filtered (completion plan F6)."""
        records = self.get_files([file_id])
        return records[0] if records else None

    def get_entities_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
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
                        (
                            {"term": {"generation": generation}}
                            if generation is not None
                            else self._generation_filter_clause()
                        ),
                    ]
                }
            },
        )
        response_dict = dict(response) if not isinstance(response, dict) else response
        return [hit["_source"] for hit in response_dict.get("hits", {}).get("hits", [])]

    def get_document_units_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
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
                        (
                            {"term": {"generation": generation}}
                            if generation is not None
                            else self._generation_filter_clause()
                        ),
                    ]
                }
            },
        )
        response_dict = dict(response) if not isinstance(response, dict) else response
        return [hit["_source"] for hit in response_dict.get("hits", {}).get("hits", [])]

    def count_stats(self) -> BackendStats:
        """``files_count`` is not generation-filtered (file identity
        documents carry no ``generation`` field); the entity/chunk/
        embedding counts are, so they reflect only the active
        generation's content, same as every other content-index read.
        """
        client = self._get_client()
        generation_clause = self._generation_filter_clause()
        files_count = _count(
            client,
            mappings.files_index(self._prefix),
            {"bool": {"filter": [{"term": {"doc_kind": "file"}}, generation_clause]}},
        )
        content_index = mappings.content_index(self._prefix)
        entities_count = _count(
            client,
            content_index,
            {"bool": {"filter": [{"term": {"doc_kind": "entity"}}, generation_clause]}},
        )
        document_units_count = _count(
            client,
            content_index,
            {"bool": {"filter": [{"term": {"doc_kind": "chunk"}}, generation_clause]}},
        )
        embeddings_count = _count(
            client,
            content_index,
            {"bool": {"filter": [{"exists": {"field": "embedding"}}, generation_clause]}},
        )
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


def _document_meta(document: Any) -> dict[str, Any]:
    return {
        "author": getattr(document, "author", None),
        "page_count": getattr(document, "page_count", None),
        "section_count": getattr(document, "section_count", 0),
        "paragraph_count": getattr(document, "paragraph_count", 0),
        "table_count": getattr(document, "table_count", 0),
        "is_scanned": bool(getattr(document, "is_scanned", False)),
    }


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
