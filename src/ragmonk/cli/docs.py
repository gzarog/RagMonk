"""``ragmonk docs [--source ID] [--json]``.

Read-only: lists ``FileKind.DOCUMENT`` files and whatever Docling-derived
metadata is available for them. A file that failed, was skipped for size,
or is an unsupported document format still appears (with its file status
and no document metadata) rather than silently disappearing from the
listing -- the full search/explore experience over documents is Phase 5's
job, not this one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ragmonk.core import paths
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import FileKind, FileRecord
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import documents_repo, files_repo

from ._common import cli_command, console, print_json


def _document_row(conn: sqlite3.Connection, source_id: str, file: FileRecord) -> dict[str, Any]:
    document = documents_repo.get_document_by_file(conn, file.id)
    return {
        "source_id": source_id,
        "file_id": file.id,
        "path": file.path,
        "status": file.status.value,
        "format": document.format.value if document is not None else None,
        "title": document.title if document is not None else None,
        "page_count": document.page_count if document is not None else None,
        "section_count": document.section_count if document is not None else 0,
        "paragraph_count": document.paragraph_count if document is not None else 0,
        "table_count": document.table_count if document is not None else 0,
        "is_scanned": document.is_scanned if document is not None else False,
    }


def _backend_document_row(
    source_id: str, file: Any, document: Any | None
) -> dict[str, Any]:
    """Server-mode counterpart of ``_document_row``, built from the
    backend-neutral ``FileRecord``/``DocumentRecord`` projections
    (``KnowledgeBackend.list_files``/``list_documents``) instead of local
    SQLite rows -- same output shape either way.
    """
    return {
        "source_id": source_id,
        "file_id": file.file_id,
        "path": file.path,
        "status": str(file.metadata.get("status", "")),
        "format": document.format if document is not None else None,
        "title": document.title if document is not None else None,
        "page_count": document.page_count if document is not None else None,
        "section_count": document.section_count if document is not None else 0,
        "paragraph_count": document.paragraph_count if document is not None else 0,
        "table_count": document.table_count if document is not None else 0,
        "is_scanned": document.is_scanned if document is not None else False,
    }


def _run_server(ctx: AppContext, sources: list[Any]) -> list[dict[str, Any]]:
    backend = ctx.backend()
    rows: list[dict[str, Any]] = []
    for source in sources:
        files = backend.list_files(source.id)
        documents = backend.list_documents(source_id=source.id)
        documents_by_file = {d.file_id: d for d in documents}
        for file in files:
            if str(file.metadata.get("kind", "")) != FileKind.DOCUMENT.value:
                continue
            rows.append(_backend_document_row(source.id, file, documents_by_file.get(file.file_id)))
    return rows


def _run(ctx: AppContext, source_id: str | None) -> list[dict[str, Any]]:
    """Shared with Phase 6's ``ragmonk_documents`` MCP tool -- the same
    file/document listing either caller sees, just shaped differently.

    Server mode (``storage.mode == "server"``) reads exclusively through
    ``ctx.backend()``'s targeted read primitives (completion plan F4) --
    never local SQLite (see ``AppContext.project_conn``'s server-mode
    guard); local mode is unchanged, byte for byte.
    """
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    sources = [registry.get(source_id)] if source_id is not None else registry.list()

    if ctx.config.storage.mode == "server":
        return _run_server(ctx, sources)

    rows: list[dict[str, Any]] = []
    for source in sources:
        project_id = paths.project_id_for_path(Path(source.path))
        # Independent review BLOCKER fix: genuine knowledge-data read
        # (document/file listing) -- default control_plane=False raises
        # cleanly in server mode. Shared with the ``ragmonk_documents``
        # MCP tool (see this function's docstring), so that surface is
        # covered by the same guard too.
        conn = ctx.project_conn(project_id)
        for file in files_repo.list_by_source(conn, source.id):
            if file.kind is not FileKind.DOCUMENT:
                continue
            rows.append(_document_row(conn, source.id, file))
    return rows


@cli_command
def docs(
    source_id: Annotated[
        str | None, typer.Option("--source", help="Only list this source id.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        rows = _run(ctx, source_id)

        if json_output:
            print_json({"documents": rows})
            return

        if not rows:
            console.print("[yellow]No indexed documents.[/yellow]")
            return

        table = Table("Path", "Format", "Status", "Pages", "Sections", "Title")
        for row in rows:
            table.add_row(
                row["path"],
                row["format"] or "-",
                row["status"],
                str(row["page_count"]) if row["page_count"] is not None else "-",
                str(row["section_count"]),
                row["title"] or "-",
            )
        console.print(table)
