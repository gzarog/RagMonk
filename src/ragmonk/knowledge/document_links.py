"""Documentation evidence for a set of matched code symbols -- the
cross-domain (code entity -> document) links ``impact`` and ``explore``
report, shared so both commands (and the MCP/Admin UI surfaces reusing
them) read links the same way in both storage modes.

Completion plan F4: local mode reads the per-project SQLite
``links``/``documents``/``document_sections``/``files`` tables exactly
as ``cli/impact.py``/``cli/explore.py`` did before; server mode reads the
same facts through the backend's targeted read primitives
(``get_links``/``get_documents``/``get_document_units``/``get_files``),
every one filtered to the published generation -- never local SQLite.
"""

from __future__ import annotations

from ragmonk.code.graph import SourceMatch, conn_for_source_path
from ragmonk.core.lifecycle import AppContext
from ragmonk.core.models import Confidence, CrossLink, RelationshipType, SectionKind
from ragmonk.knowledge import evidence as evidence_mod
from ragmonk.knowledge.evidence import Evidence
from ragmonk.storage.repositories import documents_repo, files_repo, links_repo
from ragmonk.storage.repositories.documents_repo import DocumentUnit


def linked_document_evidence(
    ctx: AppContext, matches: list[SourceMatch]
) -> list[tuple[Evidence, Confidence]]:
    """One ``(Evidence, confidence)`` per distinct (document, section)
    linked to any of ``matches``, in deterministic match/link order.
    """
    if not matches:
        return []
    if ctx.config.storage.mode == "server":
        return _server(ctx, matches)
    return _local(ctx, matches)


def _local(ctx: AppContext, matches: list[SourceMatch]) -> list[tuple[Evidence, Confidence]]:
    out: list[tuple[Evidence, Confidence]] = []
    seen: set[tuple[str, str | None]] = set()
    for match in matches:
        conn = conn_for_source_path(ctx, match.source_path)
        for link in links_repo.list_by_entity(conn, match.entity.id):
            key = (link.document_id, link.section_id)
            if key in seen:
                continue
            seen.add(key)
            document = documents_repo.get_document(conn, link.document_id)
            if document is None:
                continue
            file = files_repo.get(conn, document.file_id)
            unit = documents_repo.get_unit(conn, link.section_id) if link.section_id else None
            ev = evidence_mod.from_cross_link(
                link,
                entity=match.entity,
                document_path=file.path if file is not None else document.id,
                unit=unit,
            )
            out.append((ev, link.confidence))
    return out


def _server(ctx: AppContext, matches: list[SourceMatch]) -> list[tuple[Evidence, Confidence]]:
    backend = ctx.backend()
    by_entity = {m.entity.id: m for m in matches}
    links = backend.get_links(entity_ids=list(by_entity))
    if not links:
        return []
    documents = {d.document_id: d for d in backend.get_documents([lk.document_id for lk in links])}
    files = {f.file_id: f for f in backend.get_files([d.file_id for d in documents.values()])}
    section_ids = [lk.section_id for lk in links if lk.section_id]
    unit_records = backend.get_document_units(unit_ids=section_ids) if section_ids else []
    units = {u.unit_id: u for u in unit_records}
    out: list[tuple[Evidence, Confidence]] = []
    seen: set[tuple[str, str | None]] = set()
    # Deterministic: match order first (as local mode iterates), then the
    # link's own natural key.
    order = {entity_id: i for i, entity_id in enumerate(by_entity)}
    for link in sorted(
        links,
        key=lambda lk: (
            order.get(lk.entity_id, len(order)),
            lk.document_id,
            lk.section_id or "",
            lk.link_type,
            lk.resolver,
        ),
    ):
        key = (link.document_id, link.section_id)
        if key in seen or link.entity_id not in by_entity:
            continue
        seen.add(key)
        document = documents.get(link.document_id)
        if document is None:
            continue
        file = files.get(document.file_id)
        record = units.get(link.section_id) if link.section_id else None
        unit = (
            DocumentUnit(
                id=record.unit_id,
                document_id=record.document_id,
                file_id=record.file_id,
                kind=_section_kind(record.kind),
                text=record.text,
                heading_path=list(record.heading_path),
                page_start=record.page_start,
                page_end=record.page_end,
            )
            if record is not None
            else None
        )
        confidence = Confidence(link.confidence)
        cross_link = CrossLink(
            id=f"{link.entity_id}:{link.document_id}:{link.section_id or ''}",
            link_type=RelationshipType(link.link_type),
            entity_id=link.entity_id,
            document_id=link.document_id,
            section_id=link.section_id,
            resolver=link.resolver,
            confidence=confidence,
            evidence=link.evidence or None,
            created_at="",
        )
        ev = evidence_mod.from_cross_link(
            cross_link,
            entity=by_entity[link.entity_id].entity,
            document_path=file.path if file is not None else document.document_id,
            unit=unit,
        )
        out.append((ev, confidence))
    return out


def _section_kind(kind: str) -> SectionKind:
    try:
        return SectionKind(kind)
    except ValueError:
        return SectionKind.PARAGRAPH
