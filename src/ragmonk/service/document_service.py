"""Indexed-document and chunk inspection for the admin UI.

Admin UI plan, Phase 4 (§7): let an administrator verify exactly what
RagMonk extracted from each document. Reads the ``documents`` /
``document_sections`` / ``files`` tables of each source's ``knowledge.db``
through the existing repositories -- no new storage abstraction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import documents_repo, files_repo


def _project_conn(ctx: AppContext, source_path: str) -> Any:
    # Independent review BLOCKER fix: every caller of this helper
    # (``list_documents``, ``document_detail`` via ``_source_conn``) reads
    # genuine knowledge data (documents/chunks) -- deliberately left as
    # the default control_plane=False so it raises in server mode.
    project_id = paths.project_id_for_path(Path(source_path))
    return ctx.project_conn(project_id)


def _server_rows(ctx: AppContext, sources: list[Any]) -> list[dict[str, Any]]:
    """Completion plan F3: server-mode document listing, read entirely
    from ``ctx.backend()`` (documents + their file records, published
    generation only) -- never local SQLite.
    """
    backend = ctx.backend()
    rows: list[dict[str, Any]] = []
    for source in sources:
        documents = backend.list_documents(source_id=source.id)
        files = {f.file_id: f for f in backend.get_files([d.file_id for d in documents])}
        for document in documents:
            file_record = files.get(document.file_id)
            path = file_record.path if file_record else document.file_id
            meta = (file_record.metadata if file_record else {}) or {}
            rows.append(
                {
                    "id": document.document_id,
                    "source_id": source.id,
                    "file_id": document.file_id,
                    "path": path,
                    "name": Path(path).name,
                    "format": document.format,
                    "title": document.title or None,
                    "size": file_record.size_bytes if file_record else None,
                    "status": meta.get("status"),
                    "section_count": document.section_count,
                    "paragraph_count": document.paragraph_count,
                    "table_count": document.table_count,
                    "last_modified": meta.get("updated_at"),
                    "indexed_at": meta.get("last_indexed_at"),
                }
            )
    return rows


def list_documents(
    ctx: AppContext,
    *,
    source_id: str | None = None,
    query: str | None = None,
    fmt: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """Paginated, filterable list of indexed documents (§7.1)."""
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = registry.list()
    if source_id:
        sources = [s for s in sources if s.id == source_id]

    rows: list[dict[str, Any]] = []
    if ctx.config.storage.mode == "server":
        rows = [
            r
            for r in _server_rows(ctx, sources)
            if (not query or query.lower() in r["path"].lower())
            and (not fmt or r["format"] == fmt)
        ]
    for source in sources if ctx.config.storage.mode != "server" else []:
        conn = _project_conn(ctx, source.path)
        for document in documents_repo.list_by_source(conn, source.id):
            file_record = files_repo.get(conn, document.file_id)
            path = file_record.path if file_record else document.file_id
            if query and query.lower() not in path.lower():
                continue
            if fmt and document.format.value != fmt:
                continue
            rows.append(
                {
                    "id": document.id,
                    "source_id": source.id,
                    "file_id": document.file_id,
                    "path": path,
                    "name": Path(path).name,
                    "format": document.format.value,
                    "title": document.title,
                    "size": file_record.size if file_record else None,
                    "status": file_record.status.value if file_record else None,
                    "section_count": document.section_count,
                    "paragraph_count": document.paragraph_count,
                    "table_count": document.table_count,
                    "last_modified": file_record.updated_at if file_record else None,
                    "indexed_at": file_record.last_indexed_at if file_record else None,
                }
            )

    rows.sort(key=lambda r: r["path"])
    total = len(rows)
    page = max(1, page)
    start = (page - 1) * page_size
    return {
        "documents": rows[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "formats": sorted({r["format"] for r in rows}),
    }


def _server_document_detail(
    ctx: AppContext, source_id: str, document_id: str
) -> dict[str, Any]:
    backend = ctx.backend()
    documents = [d for d in backend.get_documents([document_id]) if d.source_id == source_id]
    if not documents:
        raise LookupError(f"no such document: {document_id}")
    document = documents[0]
    files = backend.get_files([document.file_id])
    file_record = files[0] if files else None
    units = backend.get_document_units(document_id=document_id)
    meta = (file_record.metadata if file_record else {}) or {}
    return {
        "id": document.document_id,
        "source_id": source_id,
        "path": file_record.path if file_record else document.file_id,
        "format": document.format,
        "title": document.title or None,
        "author": document.author,
        "page_count": document.page_count,
        "section_count": document.section_count,
        "paragraph_count": document.paragraph_count,
        "table_count": document.table_count,
        "is_scanned": document.is_scanned,
        "indexed_at": meta.get("last_indexed_at"),
        "chunks": [
            {
                "id": unit.unit_id,
                "kind": unit.kind,
                "heading_path": unit.heading_path,
                "page_start": unit.page_start,
                "page_end": unit.page_end,
                "text": unit.text,
                "has_embedding": unit.has_embedding,
            }
            for unit in units
        ],
    }


def document_detail(ctx: AppContext, source_id: str, document_id: str) -> dict[str, Any]:
    """One document with its extracted chunks/units (§7.2, §7.3)."""
    if ctx.config.storage.mode == "server":
        return _server_document_detail(ctx, source_id, document_id)
    conn = _source_conn(ctx, source_id)
    document = documents_repo.get_document(conn, document_id)
    if document is None:
        raise LookupError(f"no such document: {document_id}")
    file_record = files_repo.get(conn, document.file_id)
    units = documents_repo.list_units_by_file(conn, document.file_id)
    return {
        "id": document.id,
        "source_id": source_id,
        "path": file_record.path if file_record else document.file_id,
        "format": document.format.value,
        "title": document.title,
        "author": document.author,
        "page_count": document.page_count,
        "section_count": document.section_count,
        "paragraph_count": document.paragraph_count,
        "table_count": document.table_count,
        "is_scanned": document.is_scanned,
        "indexed_at": file_record.last_indexed_at if file_record else None,
        "chunks": [
            {
                "id": unit.id,
                "kind": unit.kind.value,
                "heading_path": unit.heading_path,
                "page_start": unit.page_start,
                "page_end": unit.page_end,
                "text": unit.text,
                "has_embedding": bool(unit.embedding_text),
            }
            for unit in units
        ],
    }


def _source_conn(ctx: AppContext, source_id: str) -> Any:
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    source = registry.get(source_id)
    return _project_conn(ctx, source.path)
