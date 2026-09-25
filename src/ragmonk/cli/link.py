"""``ragmonk link add|remove|list`` -- explicit, user-defined mappings
between a code entity and a document (Phase 4's cross-domain linker,
``knowledge/linker.py``, discovers links automatically; this is the
manual-correction surface the blueprint asks for on top of it).

An explicit link is stored with ``resolver="user"`` and ``confidence=
EXACT`` -- the highest-trust source in ``knowledge/linker.py``'s ladder,
and never written by the automated linker itself, so a link created here
is never overridden or contradicted by a later ``ragmonk index`` run
(see ``links_repo.insert``'s ``UNIQUE`` constraint and
``knowledge/linker.py``'s module docstring).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, CrossLink, RelationshipType, Source
from ragmonk.knowledge import entities as knowledge_entities
from ragmonk.sources.registry import SourceRegistry
from ragmonk.storage.repositories import documents_repo, entities_repo, files_repo, links_repo

from ._common import cli_command, console, print_json

app = typer.Typer(no_args_is_help=True, help="Inspect and manually correct the link graph.")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _candidate_sources(ctx: AppContext, source_id: str | None) -> list[Source]:
    registry = SourceRegistry(ctx.sources_conn, home=ctx.home)
    return [registry.get(source_id)] if source_id is not None else registry.list()


def _resolve_pair(
    ctx: AppContext, entity_ref: str, document_ref: str, source_id: str | None
) -> tuple[Source, sqlite3.Connection, Any, Any]:
    hits = []
    for source in _candidate_sources(ctx, source_id):
        project_id = paths.project_id_for_path(Path(source.path))
        # Independent review BLOCKER fix: genuine knowledge-data
        # read/write (cross-links between entities and documents) --
        # default control_plane=False raises cleanly in server mode
        # (caught by @cli_command below) instead of silently linking
        # against a stale/empty local sqlite file.
        conn = ctx.project_conn(project_id)
        entity_candidates = knowledge_entities.find_code_entity_candidates(conn, entity_ref)
        document_candidates = knowledge_entities.find_document_candidates(conn, document_ref)
        if len(entity_candidates) == 1 and len(document_candidates) == 1:
            hits.append((source, conn, entity_candidates[0], document_candidates[0]))
    if not hits:
        raise UsageError(
            f"no unambiguous match for entity '{entity_ref}' and document '{document_ref}'"
        )
    if len(hits) > 1:
        ids = ", ".join(hit[0].id for hit in hits)
        raise UsageError(f"ambiguous match across sources ({ids}); pass --source to disambiguate")
    return hits[0]


@app.command("add")
@cli_command
def add(
    entity: Annotated[str, typer.Argument(help="Entity id, name, or qualified name.")],
    document: Annotated[str, typer.Argument(help="Document id, file path, or filename.")],
    section: Annotated[
        str | None, typer.Option("--section", help="Specific section id within the document.")
    ] = None,
    source_id: Annotated[
        str | None, typer.Option("--source", help="Restrict lookup to this source id.")
    ] = None,
) -> None:
    with AppContext.bootstrap() as ctx:
        _source, conn, resolved_entity, resolved_document = _resolve_pair(
            ctx, entity, document, source_id
        )
        resolved_section_id: str | None = None
        if section is not None:
            unit = documents_repo.get_unit(conn, section)
            if unit is None or unit.document_id != resolved_document.id:
                raise UsageError(f"no such section '{section}' in document {resolved_document.id}")
            resolved_section_id = unit.id

        link = CrossLink(
            id=uuid.uuid4().hex,
            link_type=RelationshipType.DOCUMENTED_BY,
            entity_id=resolved_entity.id,
            document_id=resolved_document.id,
            section_id=resolved_section_id,
            resolver="user",
            confidence=Confidence.EXACT,
            evidence=None,
            created_at=_now(),
        )
        created = links_repo.insert(conn, link)
        if not created:
            console.print("[yellow]That link already exists.[/yellow]")
            return
        console.print(
            f"[bold green]Linked[/bold green] {resolved_entity.qualified_name} "
            f"-> {resolved_document.id} ({link.id})"
        )


@app.command("remove")
@cli_command
def remove(
    link_id: Annotated[str, typer.Argument(help="Link id, as shown by 'ragmonk link list'.")],
    source_id: Annotated[
        str | None, typer.Option("--source", help="Restrict lookup to this source id.")
    ] = None,
) -> None:
    with AppContext.bootstrap() as ctx:
        for source in _candidate_sources(ctx, source_id):
            project_id = paths.project_id_for_path(Path(source.path))
            # See the comment in ``_resolve_pair`` above.
            conn = ctx.project_conn(project_id)
            if links_repo.delete(conn, link_id):
                console.print(f"[bold red]Removed[/bold red] {link_id}")
                return
        raise UsageError(f"no such link: {link_id}")


def _link_row(conn: sqlite3.Connection, source: Source, link: CrossLink) -> dict[str, Any]:
    entity = entities_repo.get(conn, link.entity_id)
    document = documents_repo.get_document(conn, link.document_id)
    document_path = None
    if document is not None:
        file = files_repo.get(conn, document.file_id)
        document_path = file.path if file is not None else None
    return {
        "link_id": link.id,
        "link_type": link.link_type.value,
        "source_id": source.id,
        "entity_id": link.entity_id,
        "entity": entity.qualified_name if entity is not None else None,
        "document_id": link.document_id,
        "document_path": document_path,
        "section_id": link.section_id,
        "resolver": link.resolver,
        "confidence": link.confidence.value,
        "evidence": link.evidence,
    }


@app.command("list")
@cli_command
def list_links(
    entity: Annotated[
        str | None, typer.Option("--entity", help="Only links for this entity id/name.")
    ] = None,
    document_id: Annotated[
        str | None, typer.Option("--document", help="Only links for this document id.")
    ] = None,
    source_id: Annotated[
        str | None, typer.Option("--source", help="Restrict lookup to this source id.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    with AppContext.bootstrap() as ctx:
        rows: list[dict[str, Any]] = []
        for source in _candidate_sources(ctx, source_id):
            project_id = paths.project_id_for_path(Path(source.path))
            # See the comment in ``_resolve_pair`` above.
            conn = ctx.project_conn(project_id)

            if entity is not None:
                candidates = knowledge_entities.find_code_entity_candidates(conn, entity)
                entity_ids = {e.id for e in candidates}
                links = [
                    link for eid in entity_ids for link in links_repo.list_by_entity(conn, eid)
                ]
            elif document_id is not None:
                links = links_repo.list_by_document(conn, document_id)
            else:
                links = links_repo.list_all(conn)

            rows.extend(_link_row(conn, source, link) for link in links)

        if json_output:
            print_json({"links": rows})
            return

        if not rows:
            console.print("[yellow]No links found.[/yellow]")
            return

        table = Table("ID", "Type", "Entity", "Document", "Resolver", "Confidence")
        for row in rows:
            table.add_row(
                row["link_id"],
                row["link_type"],
                row["entity"] or row["entity_id"],
                row["document_path"] or row["document_id"],
                row["resolver"],
                row["confidence"],
            )
        console.print(table)
