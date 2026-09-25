"""Index names and mappings for the OpenSearch adapter.

Storage backend abstraction plan, Phase 4: three indices per configured
``index_prefix`` (default ``"ragmonk"``):

- ``{prefix}-files``: file identity records (mirrors ``FileRecord``) plus
  one small "generation marker" document per source (see
  ``opensearch.py``'s generation-lifecycle docstring).
- ``{prefix}-content``: lexically-searchable content -- code entities,
  document rows, and document chunks (sections/paragraphs/tables) --
  everything ``lexical_search``/``symbol_search`` query against. Carries
  an optional dense ``embedding`` field once semantic search is in use
  (see ``ensure_vector_field``); its dimension is not known until the
  first embeddings batch is published, so it is added lazily rather than
  declared up front with a guessed size.
- ``{prefix}-relationships``: code relationships and cross-domain links
  (``graph_neighbors`` traverses this index).

No ``opensearch-py`` import here -- mapping bodies are plain dicts, so
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
            "index.knn": True,
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
            client.indices.create(index=name, body=body)


def ensure_vector_field(client: Any, prefix: str, dims: int) -> None:
    """Lazily add a ``knn_vector`` ``embedding`` field to the content
    index once an embedding dimension is known (first
    ``publish_embeddings`` call). A no-op if the field is already
    mapped -- OpenSearch's ``put_mapping`` is safe to call repeatedly
    with the same field definition, and this backend never changes
    ``dims`` for an existing field once it exists.
    """
    index = content_index(prefix)
    current = client.indices.get_mapping(index=index)
    properties = current.get(index, {}).get("mappings", {}).get("properties", {})
    if "embedding" in properties:
        return
    client.indices.put_mapping(
        index=index,
        body={
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dims,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "nmslib",
                    },
                }
            }
        },
    )
