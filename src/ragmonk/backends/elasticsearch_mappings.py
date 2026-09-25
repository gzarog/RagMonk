"""Index names and mappings for the Elasticsearch adapter.

Storage backend abstraction plan, Phase 5: three indices per configured
``index_prefix`` (default ``"ragmonk"``) -- the same three-index-per-
prefix shape as the OpenSearch adapter, for backend-contract consistency:

- ``{prefix}-files``: file identity records (mirrors ``FileRecord``) plus
  one small "generation marker" document per source (see
  ``elasticsearch.py``'s generation-lifecycle docstring).
- ``{prefix}-content``: lexically-searchable content -- code entities,
  document rows, and document chunks (sections/paragraphs/tables) --
  everything ``lexical_search``/``symbol_search`` query against. Carries
  an optional ``dense_vector`` ``embedding`` field once semantic search
  is in use (see ``ensure_vector_field``); its dimension is not known
  until the first embeddings batch is published, so it is added lazily
  rather than declared up front with a guessed size.
- ``{prefix}-relationships``: code relationships and cross-domain links
  (``graph_neighbors`` traverses this index).

Elasticsearch's native vector field is ``dense_vector`` (with
``index: true``/``similarity`` options), not OpenSearch's k-NN-plugin
``knn_vector`` -- genuinely different mapping syntax, not just a renamed
field type, which is why this module is not shared with
``opensearch_mappings.py``.

No ``elasticsearch`` import here -- mapping bodies are plain dicts, so
this module is safe to import unconditionally.
"""

from __future__ import annotations

from typing import Any

_KEYWORD = {"type": "keyword"}
_TEXT = {"type": "text"}
_INT = {"type": "integer"}


def files_index(prefix: str) -> str:
    return f"{prefix}-files"


def content_index(prefix: str) -> str:
    return f"{prefix}-content"


def relationships_index(prefix: str) -> str:
    return f"{prefix}-relationships"


def all_indices(prefix: str) -> list[str]:
    return [files_index(prefix), content_index(prefix), relationships_index(prefix)]


def files_mapping() -> dict[str, Any]:
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "doc_kind": _KEYWORD,  # "file" | "generation_marker"
                "source_id": _KEYWORD,
                "file_id": _KEYWORD,
                "path": _KEYWORD,
                "content_hash": _KEYWORD,
                "size_bytes": _INT,
                "mtime": {"type": "double"},
                "metadata": {"type": "object", "enabled": False},
                "active_generation": _KEYWORD,
            }
        },
    }


def content_mapping() -> dict[str, Any]:
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        },
        "mappings": {
            "properties": {
                "doc_kind": _KEYWORD,  # "entity" | "document" | "chunk"
                "source_id": _KEYWORD,
                "file_id": _KEYWORD,
                "generation": _KEYWORD,
                "entity_id": _KEYWORD,
                "chunk_id": _KEYWORD,
                "document_id": _KEYWORD,
                "kind": _KEYWORD,  # entity kind / chunk kind
                "language": _KEYWORD,
                "name": _KEYWORD,
                "qualified_name": _KEYWORD,
                "path": _KEYWORD,
                "heading_path": _KEYWORD,
                "content": _TEXT,
                "search_text": _TEXT,
                "snippet": _TEXT,
                "start_line": _INT,
                "end_line": _INT,
                "created_at": _KEYWORD,
                "updated_at": _KEYWORD,
            }
        },
    }


def relationships_mapping() -> dict[str, Any]:
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "doc_kind": _KEYWORD,  # "relationship" | "link"
                "source_id": _KEYWORD,
                "file_id": _KEYWORD,
                "generation": _KEYWORD,
                "relationship_type": _KEYWORD,
                "source_entity_id": _KEYWORD,
                "target_entity_id": _KEYWORD,
                "target_symbol": _KEYWORD,
                "entity_id": _KEYWORD,
                "document_id": _KEYWORD,
                "section_id": _KEYWORD,
                "resolver": _KEYWORD,
                "confidence": _KEYWORD,
                "evidence": _TEXT,
                "created_at": _KEYWORD,
            }
        },
    }


def ensure_schema(client: Any, prefix: str) -> None:
    """Idempotently create the three indices with their mappings.
    Existence is checked first so repeated calls (a fresh
    ``ensure_schema`` on every backend construction) are safe no-ops.
    """
    for name, body in (
        (files_index(prefix), files_mapping()),
        (content_index(prefix), content_mapping()),
        (relationships_index(prefix), relationships_mapping()),
    ):
        if not client.indices.exists(index=name):
            settings = body["settings"]
            mappings = body["mappings"]
            client.indices.create(index=name, settings=settings, mappings=mappings)


def ensure_vector_field(client: Any, prefix: str, dims: int) -> None:
    """Lazily add a native ``dense_vector`` ``embedding`` field to the
    content index once an embedding dimension is known (first
    ``publish_embeddings`` call). A no-op if the field is already
    mapped -- Elasticsearch's ``put_mapping`` is safe to call repeatedly
    with the same field definition, and this backend never changes
    ``dims`` for an existing field once it exists.

    ``index: true`` with ``similarity: "cosine"`` is what makes the field
    queryable by a native ``knn`` query (see ``semantic_search``) --
    Elasticsearch's own approximate-nearest-neighbor search, distinct
    from OpenSearch's k-NN plugin/``knn_vector`` approach.
    """
    index = content_index(prefix)
    current = client.indices.get_mapping(index=index)
    index_body = current.get(index) or {}
    properties = index_body.get("mappings", {}).get("properties", {})
    if "embedding" in properties:
        return
    client.indices.put_mapping(
        index=index,
        properties={
            "embedding": {
                "type": "dense_vector",
                "dims": dims,
                "index": True,
                "similarity": "cosine",
            }
        },
    )
