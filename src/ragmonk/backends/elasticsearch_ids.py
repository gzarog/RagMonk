"""Deterministic ``_id`` derivation for the Elasticsearch adapter.

Storage backend abstraction plan, Phase 5: Elasticsearch must never be
left to auto-generate ``_id`` for a document derived from RagMonk's own
entities/relationships/chunks/links -- an auto id would make re-indexing
the same file (a rebuild, a re-run after an edit) create duplicates
instead of idempotently overwriting the previous document. Every id here
is a pure function of stable, already-known identifiers (``source_id``,
``file_id``, an already-assigned local/entity/chunk id, or -- for link
candidates, which carry no id of their own -- their natural key), so the
same logical row always maps to the same ``_id`` no matter how many times
it is published.

This module intentionally mirrors ``opensearch_ids.py``'s hashing scheme
(same shape of problem, same solution) but is implemented independently
with no import of that sibling module -- see the project's "keep
OpenSearch and Elasticsearch adapters separate" architecture note.

This module has no Elasticsearch import and no ``elasticsearch``
dependency -- it is pure string/hash logic, safe to import
unconditionally.
"""

from __future__ import annotations

import hashlib

_SEP = "\x1f"  # ASCII unit separator: never appears in any part we join.


def _digest(*parts: str) -> str:
    """A stable, URL/id-safe hex digest of ``parts`` joined by a
    separator byte that cannot collide with any part's own content
    (paths, names, etc. may contain ``:``/``/`` freely).
    """
    joined = _SEP.join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()  # noqa: S324 -- id derivation, not security


def file_doc_id(source_id: str, file_id: str) -> str:
    """The ``{prefix}-files`` document id for one file's identity record."""
    return _digest("file", source_id, file_id)


def generation_marker_id(source_id: str) -> str:
    """The ``{prefix}-files`` document id for ``source_id``'s active-
    generation marker (see ``elasticsearch.py``'s generation lifecycle
    docstring for what this marker means).
    """
    return _digest("generation-marker", source_id)


def document_doc_id(source_id: str, file_id: str) -> str:
    """The ``{prefix}-content`` id for a file's ``Document`` row."""
    return _digest("document", source_id, file_id)


def entity_doc_id(source_id: str, file_id: str, entity_id: str) -> str:
    """The ``{prefix}-content`` id for one code entity."""
    return _digest("entity", source_id, file_id, entity_id)


def chunk_doc_id(source_id: str, file_id: str, chunk_id: str) -> str:
    """The ``{prefix}-content`` id for one document chunk (section/
    paragraph/table).
    """
    return _digest("chunk", source_id, file_id, chunk_id)


def relationship_doc_id(source_id: str, file_id: str, relationship_id: str) -> str:
    """The ``{prefix}-relationships`` id for one code relationship."""
    return _digest("relationship", source_id, file_id, relationship_id)


def link_doc_id(
    source_id: str,
    entity_id: str,
    document_id: str,
    section_id: str | None,
    link_type: str,
    resolver: str,
) -> str:
    """The ``{prefix}-relationships`` id for one cross-domain link.

    ``LinkCandidate`` carries no id of its own -- this mirrors the same
    natural-key uniqueness ``links_repo.insert`` enforces locally
    (entity, document, section, link type, resolver), so publishing the
    same candidate twice overwrites rather than duplicates.
    """
    return _digest(
        "link", source_id, entity_id, document_id, section_id or "", link_type, resolver
    )
