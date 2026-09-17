"""One consistent way to look up a known entity -- code or document -- by
id or by canonical/qualified name, within a single project's
``knowledge.db``.

A query facade, not a new persistence layer: every function here wraps
``entities_repo``/``documents_repo``/``files_repo`` (already Phase 2/3's
storage access points) rather than reading tables directly. ``cli/link.py``
uses this to resolve what a person typed on the command line; Phase 5's
context builder is expected to build on the same functions rather than
re-implementing "is this a code id or a document id" dispatch.

Scope: one project (``knowledge.db``) at a time, matching Phase 2/3's own
per-project connection scoping -- code/graph.py's ``all_project_connections``
is what a caller reaches for when a lookup needs to span every registered
source (e.g. ``cli/link.py`` iterating sources to disambiguate a name).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ragmonk.core.models import Document, Entity
from ragmonk.storage.repositories import documents_repo, entities_repo, files_repo


def find_code_entity_candidates(conn: sqlite3.Connection, ref: str) -> list[Entity]:
    """Every code entity ``ref`` could mean: an exact id match if ``ref``
    happens to be one, else every entity whose bare name or qualified
    name equals ``ref`` (``entities_repo.search``). Callers decide what
    to do with more than one candidate (typically: ask for something more
    specific).
    """
    direct = entities_repo.get(conn, ref)
    if direct is not None:
        return [direct]
    return entities_repo.search(conn, ref)


def resolve_code_entity(conn: sqlite3.Connection, ref: str) -> Entity | None:
    """``find_code_entity_candidates``, collapsed to a single result --
    ``None`` for both "no match" and "ambiguous, more than one match".
    """
    candidates = find_code_entity_candidates(conn, ref)
    return candidates[0] if len(candidates) == 1 else None


def find_document_candidates(conn: sqlite3.Connection, ref: str) -> list[Document]:
    """Every document ``ref`` could mean: an exact document id, else any
    document whose underlying file path equals, ends with, or has a
    filename equal to ``ref`` -- so a person can type a document id, a
    full path, or just "report.pdf" (there is no document-level
    "canonical name" the way code entities have a qualified name; a file
    path is the closest equivalent Phase 3 stores).
    """
    direct = documents_repo.get_document(conn, ref)
    if direct is not None:
        return [direct]
    candidates: list[Document] = []
    for document in documents_repo.list_all(conn):
        file = files_repo.get(conn, document.file_id)
        if file is None:
            continue
        if file.path == ref or file.path.endswith(ref) or Path(file.path).name == ref:
            candidates.append(document)
    return candidates


def resolve_document(conn: sqlite3.Connection, ref: str) -> Document | None:
    """``find_document_candidates``, collapsed to a single result."""
    candidates = find_document_candidates(conn, ref)
    return candidates[0] if len(candidates) == 1 else None
