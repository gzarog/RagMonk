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

Server mode (``storage.mode == "server"``) never reads/writes local
SQLite here -- every lookup and mutation goes through
``ctx.backend()``'s targeted read primitives (completion plan F4) plus
``publish_links``/``remove_link``. A manual link's identity (its natural
key: entity, document, section, link type, resolver) and its
idempotency/dedup behaviour match local mode exactly -- server ids are
just the deterministic hash of that same natural key (see
``opensearch_ids.link_doc_id``/``elasticsearch_ids.link_doc_id``) instead
of a random uuid, and ``publish_links``/``remove_link`` are scoped to one
source's currently published generation, so removing a link can never
touch a different source's, a different generation's, or a different
link's row.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from ragmonk.backends.base import KnowledgeBackend
from ragmonk.backends.models import LinkCandidate, LinkRecord, PreparedLinks
from ragmonk.core import paths
from ragmonk.core.errors import UsageError
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, CrossLink, Entity, RelationshipType, Source
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


# -- server-mode entity/document resolution ---------------------------------
# Backend-neutral counterparts of ``knowledge/entities.py``'s
# ``find_code_entity_candidates``/``find_document_candidates``, built from
# ``KnowledgeBackend``'s targeted read primitives instead of a project's
# ``knowledge.db``. Same matching rules: an exact id short-circuits, else
# a code entity matches by bare or qualified name, and a document matches
# by its file's full path, path suffix, or bare filename.


def _find_code_entity_candidates_backend(
    backend: KnowledgeBackend, source_id: str, ref: str
) -> list[Entity]:
    direct = [e for e in backend.get_entities([ref]) if e.source_id == source_id]
    if direct:
        return direct
    return backend.find_entities_by_names(names=[ref], qualified_names=[ref], source_id=source_id)


def _find_document_candidates_backend(
    backend: KnowledgeBackend, source_id: str, ref: str
) -> list[Any]:
    direct = [d for d in backend.get_documents([ref]) if d.source_id == source_id]
    if direct:
        return direct
    documents = backend.list_documents(source_id=source_id)
    if not documents:
        return []
    files_by_id = {f.file_id: f for f in backend.get_files([d.file_id for d in documents])}
    candidates = []
    for document in documents:
        file = files_by_id.get(document.file_id)
        if file is None:
            continue
        if file.path == ref or file.path.endswith(ref) or Path(file.path).name == ref:
            candidates.append(document)
    return candidates


def _resolve_pair_server(
    ctx: AppContext, entity_ref: str, document_ref: str, source_id: str | None
) -> tuple[Source, Entity, Any]:
    backend = ctx.backend()
    hits = []
    for source in _candidate_sources(ctx, source_id):
        entity_candidates = _find_code_entity_candidates_backend(backend, source.id, entity_ref)
        document_candidates = _find_document_candidates_backend(backend, source.id, document_ref)
        if len(entity_candidates) == 1 and len(document_candidates) == 1:
            hits.append((source, entity_candidates[0], document_candidates[0]))
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
        if ctx.config.storage.mode == "server":
            source, resolved_entity, resolved_document = _resolve_pair_server(
                ctx, entity, document, source_id
            )
            backend = ctx.backend()
            resolved_section_id: str | None = None
            if section is not None:
                units = backend.get_document_units(document_id=resolved_document.document_id)
                matched_unit = next((u for u in units if u.unit_id == section), None)
                if matched_unit is None:
                    raise UsageError(
                        f"no such section '{section}' in document {resolved_document.document_id}"
                    )
                resolved_section_id = matched_unit.unit_id

            candidate = LinkCandidate(
                entity_id=resolved_entity.id,
                document_id=resolved_document.document_id,
                section_id=resolved_section_id,
                link_type=RelationshipType.DOCUMENTED_BY,
                resolver="user",
                confidence=Confidence.EXACT,
                evidence="",
            )
            # ``generation=None``: write into the source's currently
            # *published* generation directly (see ``_write_generation``
            # on the server adapters) so the link is visible immediately,
            # exactly like local mode's immediate sqlite insert -- never
            # a separate begin/publish-generation round trip for one
            # manual link.
            created = backend.publish_links(
                PreparedLinks(source_id=source.id, candidates=[candidate], generation=None)
            )
            if not created:
                console.print("[yellow]That link already exists.[/yellow]")
                return
            link_id = ""
            for candidate_link in backend.get_links(entity_ids=[resolved_entity.id]):
                if (
                    candidate_link.document_id == resolved_document.document_id
                    and candidate_link.section_id == resolved_section_id
                ):
                    link_id = candidate_link.id
                    break
            suffix = f" ({link_id})" if link_id else ""
            console.print(
                f"[bold green]Linked[/bold green] {resolved_entity.qualified_name} "
                f"-> {resolved_document.document_id}{suffix}"
            )
            return

        _source, conn, resolved_entity_local, resolved_document_local = _resolve_pair(
            ctx, entity, document, source_id
        )
        resolved_section_id_local: str | None = None
        if section is not None:
            local_unit = documents_repo.get_unit(conn, section)
            if local_unit is None or local_unit.document_id != resolved_document_local.id:
                raise UsageError(
                    f"no such section '{section}' in document {resolved_document_local.id}"
                )
            resolved_section_id_local = local_unit.id

        local_link = CrossLink(
            id=uuid.uuid4().hex,
            link_type=RelationshipType.DOCUMENTED_BY,
            entity_id=resolved_entity_local.id,
            document_id=resolved_document_local.id,
            section_id=resolved_section_id_local,
            resolver="user",
            confidence=Confidence.EXACT,
            evidence=None,
            created_at=_now(),
        )
        created_local = links_repo.insert(conn, local_link)
        if not created_local:
            console.print("[yellow]That link already exists.[/yellow]")
            return
        console.print(
            f"[bold green]Linked[/bold green] {resolved_entity_local.qualified_name} "
            f"-> {resolved_document_local.id} ({local_link.id})"
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
        if ctx.config.storage.mode == "server":
            # ``source_id`` is accepted for CLI symmetry with local mode
            # but not needed to disambiguate: a server link id already
            # encodes its source/entity/document/section/type/resolver
            # (see ``link_doc_id``), so ``remove_link`` can never match --
            # let alone delete -- a link belonging to a different source,
            # generation, or link identity.
            if ctx.backend().remove_link(link_id):
                console.print(f"[bold red]Removed[/bold red] {link_id}")
                return
            raise UsageError(f"no such link: {link_id}")

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


def _backend_link_row(
    backend: KnowledgeBackend, source: Source, link: LinkRecord
) -> dict[str, Any]:
    entities = backend.get_entities([link.entity_id])
    entity = entities[0] if entities else None
    documents = backend.get_documents([link.document_id])
    document = documents[0] if documents else None
    document_path = None
    if document is not None:
        files = backend.get_files([document.file_id])
        document_path = files[0].path if files else None
    return {
        "link_id": link.id,
        "link_type": link.link_type,
        "source_id": source.id,
        "entity_id": link.entity_id,
        "entity": entity.qualified_name if entity is not None else None,
        "document_id": link.document_id,
        "document_path": document_path,
        "section_id": link.section_id,
        "resolver": link.resolver,
        "confidence": link.confidence,
        "evidence": link.evidence,
    }


def _list_links_server(
    ctx: AppContext, entity: str | None, document_id: str | None, source_id: str | None
) -> list[dict[str, Any]]:
    backend = ctx.backend()
    rows: list[dict[str, Any]] = []
    for source in _candidate_sources(ctx, source_id):
        if entity is not None:
            candidates = _find_code_entity_candidates_backend(backend, source.id, entity)
            entity_ids = [e.id for e in candidates]
            links = backend.get_links(entity_ids=entity_ids) if entity_ids else []
        elif document_id is not None:
            links = backend.get_links(document_ids=[document_id])
        else:
            # No "list every link of a source" primitive exists (nor is
            # one needed elsewhere) -- every link points at a document,
            # so querying by every document id of this source covers the
            # same set ``links_repo.list_all`` would, scoped to this
            # source, without adding a new backend method for it.
            document_ids = [d.document_id for d in backend.list_documents(source_id=source.id)]
            links = backend.get_links(document_ids=document_ids) if document_ids else []
        rows.extend(_backend_link_row(backend, source, link) for link in links)
    return rows


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
        if ctx.config.storage.mode == "server":
            rows = _list_links_server(ctx, entity, document_id, source_id)
        else:
            rows = []
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
