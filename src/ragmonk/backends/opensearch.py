"""``OpenSearchKnowledgeBackend`` -- a ``KnowledgeBackend`` adapter for a
server-mode OpenSearch cluster.

Storage backend abstraction plan, Phase 4. ``opensearch-py`` stays an
OPTIONAL dependency: nothing at this module's top level imports it --
only ``opensearch_client.build_client`` does, lazily, inside a function
body -- so ``import ragmonk.backends.opensearch`` and constructing a
*local*-mode backend via ``ragmonk.backends.factory.create_backend``
never requires it to be installed. Constructing *this* class does need
it (raises :class:`~ragmonk.backends.opensearch_client.OpenSearchDependencyError`
with a clear install hint if it's missing).

Index design (see ``opensearch_mappings.py`` for the exact mappings):
``{prefix}-files``, ``{prefix}-content``, ``{prefix}-relationships``.
Every write goes through the bounded bulk helper in ``opensearch_bulk.py``
-- never a per-entity/per-chunk HTTP request -- using deterministic ids
from ``opensearch_ids.py`` so re-publishing the same file is an idempotent
overwrite, not a duplicate.

Generation (rebuild-safety) design: rather than swapping index aliases
(which would require one physical index *per generation*, expensive to
create/drop for every incremental re-index of a single file), every
content/relationship document written by ``publish_code``/
``publish_document`` is tagged with a ``generation`` keyword field, and
each source's *currently active* generation is recorded in one small
"generation marker" document in the files index (id derived by
``opensearch_ids.generation_marker_id``, so resolving it is a single
by-id ``get``, not a search). Every read method that queries the content
or relationships index (``lexical_search``/``semantic_search``/
``symbol_search``/``graph_neighbors``/``get_entities_for_files``/
``get_document_units_for_files``/``count_stats``) builds a filter clause
from ``_active_generations_map``/``_generation_filter_clause`` that
constrains matches to each source's currently-published generation, so
an in-progress (not yet published) generation's documents are never
returned by any read -- the same externally-visible effect as an alias
swap, achieved with a single atomic document write
(``publish_generation``) instead of an index-level operation.

Completion plan F6: *every* derived artifact is generation-tagged --
file records (``upsert_file(s)``) and cross-domain links
(``publish_links``) included -- and the ids of documents that are
otherwise stable across generations (file record, document row, link)
include the generation, so a rebuild never overwrites the published
copy. A write only deletes the previous documents *of the generation it
is writing*; ``publish_generation`` flips the marker and then
garbage-collects every other generation of that source; and
``abort_generation`` deletes every artifact of the aborted generation in
all three indices, leaving the published one untouched. Documents with no
``generation`` field (written by a pre-F6 release) still pass the read
filter. ``clear_source`` is deliberately NOT generation-filtered -- it is
a full-source delete of every generation.

Targeted read primitives (completion plan F4 -- ``get_entities``,
``get_files``, ``get_links``, ``get_documents``, ...) come from
``server_common.ServerReadMixin``, shared with the Elasticsearch adapter.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ragmonk.backends import opensearch_ids as ids
from ragmonk.backends import opensearch_mappings as mappings
from ragmonk.backends.base import GraphDirection, KnowledgeBackend
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
from ragmonk.backends.opensearch_bulk import BulkAction, run_bulk_or_raise
from ragmonk.backends.opensearch_client import (
    OpenSearchConnectionError,
    build_client,
    cluster_health,
)
from ragmonk.backends.server_common import ServerReadMixin
from ragmonk.core.config import ServerStorageConfig
from ragmonk.core.models import EmbeddingSubjectType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from opensearchpy import OpenSearch

_DEFAULT_ACTIVE_GENERATION = "0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class OpenSearchKnowledgeBackend(ServerReadMixin, KnowledgeBackend):
    """A ``KnowledgeBackend`` backed by a real OpenSearch cluster.

    Never falls back to local storage: every method either performs a
    real OpenSearch call or raises. A cluster that is unreachable or
    misconfigured surfaces as an :class:`OpenSearchConnectionError` (a
    :class:`~ragmonk.core.errors.HealthCheckError`) or
    :class:`~ragmonk.backends.opensearch_bulk.BulkIndexError` (a
    :class:`~ragmonk.core.errors.DatabaseError`) -- both typed, both
    carrying no credential value -- never an empty/partial result that
    could be mistaken for "nothing found".
    """

    def __init__(self, config: ServerStorageConfig, *, client: OpenSearch | None = None) -> None:
        self._config = config
        self._prefix = config.index_prefix
        self._client_override = client
        self._client: OpenSearch | None = client
        self._vector_dims: int | None = None

    # -- client -----------------------------------------------------------
    def _get_client(self) -> OpenSearch:
        if self._client is None:
            username_var, password_var, api_key_var = credential_env_vars("opensearch")
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
        body: dict[str, Any] = {"size": size, "query": query}
        if sort is not None:
            body["sort"] = sort
        if search_after is not None:
            body["search_after"] = search_after
        response = self._get_client().search(index=index, body=body)
        hits: list[dict[str, Any]] = response.get("hits", {}).get("hits", [])
        return hits

    def _delete_by_query_raw(self, index: str, query: dict[str, Any]) -> None:
        self._get_client().delete_by_query(
            index=index, body={"query": query}, refresh=True, conflicts="proceed"
        )

    # -- lifecycle -----------------------------------------------------
    def health(self) -> bool:
        """Returns True if reachable; False only for a graceful
        "cannot reach"/"info malformed" condition -- ``cluster_health``
        itself still raises for a hard failure, which callers wanting the
        engine name/version (rather than a bool) should catch, e.g. via
        :meth:`describe`.
        """
        try:
            self.describe()
            return True
        except OpenSearchConnectionError:
            return False

    def describe(self) -> tuple[str, str]:
        """Returns ``(engine_name, version)`` from a real cluster call.
        Raises :class:`OpenSearchConnectionError` on failure.
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
        return doc.get("_source", {}) if isinstance(doc, dict) else {}

    def _active_generation(self, source_id: str) -> str:
        source = self._generation_marker_source(source_id)
        return str(source.get("active_generation", _DEFAULT_ACTIVE_GENERATION))

    def _active_generations_map(self) -> dict[str, str]:
        """Every source's currently-active generation, keyed by
        ``source_id``. A single bounded search over the (small) set of
        generation-marker documents in the files index -- not one call
        per source -- so a read method that isn't pre-scoped to a
        specific source (``lexical_search``/``semantic_search``/etc.)
        can still resolve every relevant source's active generation in
        one round trip.
        """
        client = self._get_client()
        body = {"size": 10_000, "query": {"term": {"doc_kind": "generation_marker"}}}
        try:
            response = client.search(index=mappings.files_index(self._prefix), body=body)
        except Exception:
            return {}
        hits = response.get("hits", {}).get("hits", [])
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
        currently-published generation.

        For every source with an explicit marker, only documents tagged
        with that source's active generation match. For a source with
        no marker yet (never published), the default active generation
        (``_DEFAULT_ACTIVE_GENERATION``) is used -- matching
        ``_active_generation``'s fallback, so a first-ever rebuild's
        pre-publish documents (tagged with generation ``"1"``) are
        correctly invisible until ``publish_generation`` runs, exactly
        like an established source's in-progress rebuild. A document
        with no ``generation`` field at all (a cross-domain link, which
        is not rebuild-versioned) always matches, since there is no
        generation for it to be stale against.
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
        tagged with it, or those could leak into the newly-published
        generation for any file the retry's pass never revisits, such as
        one deleted from the source between attempts).

        To do this without a shared/global counter (which would break
        the per-source isolation the rest of this generation scheme
        relies on), the marker document persists not just the
        *published* ``active_generation`` but also the highest
        generation ever *begun* (``last_begun_generation``), written
        eagerly by this method itself -- before any content is touched,
        let alone published. The next id is always one past
        ``max(active_generation, last_begun_generation)``, so calling
        this twice in a row (as a retry after any failure does, since
        ``publish_generation`` never ran) always advances, never repeats.
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
            body={
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
            body={
                "doc_kind": "generation_marker",
                "source_id": source_id,
                "active_generation": generation,
                "last_begun_generation": generation,
                "updated_at": _now(),
            },
        )
        for index in mappings.all_indices(self._prefix):
            client.indices.refresh(index=index)
        # Completion plan F6: the marker flip above is the single atomic
        # visibility switch; only afterwards are the now-unreadable older
        # generations' documents (files, content, relationships, links)
        # reclaimed. A failure here leaves only invisible garbage.
        self._gc_other_generations(source_id, generation)

    def abort_generation(self, source_id: str, generation: str) -> None:
        """Deletes every artifact tagged with the aborted ``generation``
        in *every* index -- file records and cross-domain links included
        (completion plan F6), not just content/relationships -- without
        touching the marker, so the previously published generation stays
        exactly as it was.
        """
        self._delete_generation(source_id, generation)

    # -- writes -----------------------------------------------------------
    def upsert_file(self, file_record: FileRecord) -> None:
        self.upsert_files([file_record])

    def upsert_files(self, file_records: list[FileRecord]) -> None:
        """Completion plan F6: file records are generation-tagged (and
        their ``_id`` includes the generation) so a rebuild writes a
        *separate* file listing that only becomes visible on publish.
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
        """Removes ``file_id``'s file record, entities, chunks, document,
        relationships and every cross-domain link that references one of
        its entities/documents -- across all generations (a deleted file
        must not survive in any of them).
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
            client.delete_by_query(
                index=index, body={"query": scoped}, refresh=True, conflicts="proceed"
            )
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
            body={"query": links},
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
        # Completion plan F6: the delete half is scoped to the generation
        # being *written* -- a rebuild writing generation N+1 must never
        # remove generation N's documents (still the published, readable
        # ones until ``publish_generation``).
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
        # Completion plan F6: links are generation-versioned like every
        # other derived artifact, and carry the file ids of both ends so
        # a re-published/deleted file's stale links can be removed.
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
        query = {"query": {"term": {"source_id": source_id}}}
        for index in mappings.all_indices(self._prefix):
            client.delete_by_query(index=index, body=query, refresh=True, conflicts="proceed")

    # -- internal helpers ---------------------------------------------------
    def _delete_file_scoped(
        self, index: str, source_id: str, file_id: str, doc_kind: str, generation: str
    ) -> None:
        client = self._get_client()
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"source_id": source_id}},
                        {"term": {"file_id": file_id}},
                        {"term": {"doc_kind": doc_kind}},
                        {"term": {"generation": generation}},
                    ]
                }
            }
        }
        client.delete_by_query(index=index, body=query, refresh=True, conflicts="proceed")

    def _delete_links_for_file(
        self, source_id: str, file_id: str, field: str, generation: str
    ) -> None:
        """Removes this generation's cross-domain links whose code
        (``entity_file_id``) or document (``document_file_id``) end
        belongs to ``file_id`` -- the server equivalent of local mode's
        ``links`` FK cascade when a file's entities/sections are replaced.
        """
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"source_id": source_id}},
                        {"term": {"doc_kind": "link"}},
                        {"term": {field: file_id}},
                        {"term": {"generation": generation}},
                    ]
                }
            }
        }
        self._get_client().delete_by_query(
            index=mappings.relationships_index(self._prefix),
            body=query,
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
        body = {
            "size": limit,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }
        response = self._get_client().search(index=mappings.content_index(self._prefix), body=body)
        return _hits_to_search_hits(response)

    def semantic_search(
        self, vector: list[float], limit: int, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
        body = {
            "size": limit,
            "query": {
                "bool": {
                    "must": [{"knn": {"embedding": {"vector": vector, "k": limit}}}],
                    "filter": filter_clauses,
                }
            },
        }
        response = self._get_client().search(index=mappings.content_index(self._prefix), body=body)
        return _hits_to_search_hits(response)

    def symbol_search(
        self, name: str, filters: dict[str, Any] | None = None
    ) -> list[SearchHit]:
        must = [
            {"term": {"doc_kind": "entity"}},
            {"bool": {"should": [{"term": {"name": name}}, {"term": {"qualified_name": name}}]}},
        ]
        filter_clauses = self._filter_clauses(filters) + [self._generation_filter_clause()]
        body = {
            "size": 50,
            "query": {"bool": {"must": must, "filter": filter_clauses}},
        }
        response = self._get_client().search(index=mappings.content_index(self._prefix), body=body)
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
        for current_depth in range(1, max(depth, 1) + 1):
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
            body = {
                "size": 500,
                "query": {
                    "bool": {
                        "should": should,
                        "minimum_should_match": 1,
                        "filter": filter_clauses,
                    }
                },
            }
            response = client.search(index=index, body=body)
            hits = _hits_to_search_hits(response)
            next_frontier: set[str] = set()
            for hit in hits:
                if hit.id not in seen:
                    seen.add(hit.id)
                    # Real hop distance from the traversal root, not a
                    # hardcoded 1 -- callers (code/graph.py,
                    # retrieval/graph.py) read this back via
                    # ``hit.payload.get("_hop_depth", 1)`` instead of
                    # flattening every edge to depth=1.
                    hit.payload["_hop_depth"] = current_depth
                    results.append(hit)
                target = hit.payload.get("target_entity_id")
                source = hit.payload.get("source_entity_id")
                for candidate in (target, source):
                    if candidate and candidate not in seen:
                        next_frontier.add(candidate)
            frontier = next_frontier
        return results

    def get_file(self, file_id: str) -> FileRecord | None:
        """Generation-filtered (completion plan F6): only the published
        generation's file record is visible.
        """
        records = self.get_files([file_id])
        return records[0] if records else None

    def get_entities_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        body = {
            "size": 10_000,
            "query": {
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
        }
        response = self._get_client().search(index=mappings.content_index(self._prefix), body=body)
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]

    def get_document_units_for_files(
        self, file_ids: list[str], *, generation: str | None = None
    ) -> list[dict[str, Any]]:
        if not file_ids:
            return []
        body = {
            "size": 10_000,
            "query": {
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
        }
        response = self._get_client().search(index=mappings.content_index(self._prefix), body=body)
        return [hit["_source"] for hit in response.get("hits", {}).get("hits", [])]

    def count_stats(self) -> BackendStats:
        """``files_count`` is not generation-filtered (file identity
        documents carry no ``generation`` field); the entity/chunk/
        embedding counts are, so they reflect only the active
        generation's content, same as every other content-index read.
        """
        client = self._get_client()
        generation_clause = self._generation_filter_clause()
        files_count = client.count(
            index=mappings.files_index(self._prefix),
            body={
                "query": {"bool": {"filter": [{"term": {"doc_kind": "file"}}, generation_clause]}}
            },
        )["count"]
        content_index = mappings.content_index(self._prefix)
        entities_count = client.count(
            index=content_index,
            body={
                "query": {
                    "bool": {"filter": [{"term": {"doc_kind": "entity"}}, generation_clause]}
                }
            },
        )["count"]
        document_units_count = client.count(
            index=content_index,
            body={
                "query": {"bool": {"filter": [{"term": {"doc_kind": "chunk"}}, generation_clause]}}
            },
        )["count"]
        embeddings_count = client.count(
            index=content_index,
            body={
                "query": {
                    "bool": {"filter": [{"exists": {"field": "embedding"}}, generation_clause]}
                }
            },
        )["count"]
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


def _hits_to_search_hits(response: dict[str, Any]) -> list[SearchHit]:
    hits = response.get("hits", {}).get("hits", [])
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
