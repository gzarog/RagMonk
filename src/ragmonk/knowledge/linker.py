"""Cross-domain linker: connects code entities to the documents that
describe them (blueprint section 19, "Cross-Domain Linking").

Confidence ladder -- extends Phase 2's ``Confidence``
(``core/models.py``), same comment style as ``code/resolver.py``:

  EXACT      Never assigned by this module. Reserved for an explicit,
             user-defined mapping (``ragmonk link add``, see
             ``cli/link.py``) -- the highest-trust source, and per the
             blueprint the automated linker must never override or
             contradict a fact a person pinned by hand. This module has
             no code path that writes ``resolver="user"``, so it
             structurally cannot produce an EXACT link.
  HIGH       An exact or qualified identifier match: the entity's bare
             name or fully-qualified name appears verbatim in a
             document's heading/paragraph/table text. The blueprint asks
             for qualified matches to be "at least as strong as" bare
             matches, not a separate tier, so both are scored HIGH here;
             they differ instead in relationship type (see below) and in
             their ``resolver`` string, which keeps them distinguishable
             for audit without inventing a fifth confidence tier.
  MEDIUM     A weaker structural signal: the document names the entity's
             source filename, or a short alias of its qualified name
             (its final two dotted segments, dropping the namespace
             prefix) -- plausible, not identifier-precise.
  HEURISTIC  A framework-pattern route match, reusing Phase 2's
             ``framework_rules``-tagged HTTP endpoint findings. A naming-
             convention guess, never a language fact -- exactly the rule
             ``framework_rules.py`` itself already applies to those
             findings, just carried through to their document side too.

Relationship type marks *how strong a claim* a match is, independent of
confidence: ``DOCUMENTED_BY`` for a qualified-name match (this document
is very likely specifically about this entity), ``MENTIONED_IN`` for a
weaker/generic mention (bare name, filename, alias), ``RELATED_TO`` as
the catch-all for an ambiguous/heuristic match (route findings). Only
these three of the blueprint's relationship catalog are populated here --
PRODUCES/CONSUMES/DEPENDS_ON/REQUIRES/TESTED_BY/AFFECTS/SUPERSEDES have no
real signal in what Phases 1-3 extract.

Semantic similarity is deliberately never a source here (blueprint
sections 19 and 57): it may only ever *suggest* a link for a human to
confirm, never mint a confidence-scored fact on its own -- and Phase 4
has no embedding infrastructure to do so anyway (that is Phase 9).
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.core.models import (
    Confidence,
    CrossLink,
    Entity,
    EntityType,
    Relationship,
    RelationshipType,
)
from ragmonk.storage.repositories import (
    documents_repo,
    entities_repo,
    files_repo,
    links_repo,
    relationships_repo,
)
from ragmonk.storage.repositories.documents_repo import DocumentUnit

_ROUTE_TARGET_PREFIX = "http_endpoint:"
_MIN_ALIAS_SEGMENTS = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class LinkCandidate:
    entity_id: str
    document_id: str
    section_id: str | None
    link_type: RelationshipType
    resolver: str
    confidence: Confidence
    evidence: str


def _needle_pattern(needle: str) -> re.Pattern[str] | None:
    """Compiles ``needle``'s whole-identifier boundary pattern once --
    indexing optimization plan Phase P6: every matcher below calls this
    exactly once per distinct needle (once per entity, or once per
    filename candidate), *outside* its inner per-document-unit loop, and
    reuses the same compiled ``re.Pattern`` across every unit -- instead
    of re-building the same pattern string (a fresh ``re.escape`` call)
    on every single (entity, unit) pair, the pre-P6 shape. ``None`` for
    an empty needle keeps ``_word_present`` a plain, always-``False``
    check without a special case at every call site.

    A plain ``\\b...\\b`` regex boundary is not quite right for dotted
    qualified names -- ``\\b`` treats ``.`` itself as a boundary, so it
    would still let "Dog.bark" match inside a longer dotted path segment;
    excluding both word characters *and* ``.`` on either side is what
    actually pins the match to a standalone occurrence.
    """
    if not needle:
        return None
    return re.compile(r"(?<![\w.])" + re.escape(needle) + r"(?![\w.])")


def _word_present(text: str, pattern: re.Pattern[str] | None) -> bool:
    return pattern is not None and pattern.search(text) is not None


def match_exact_identifier(
    entities: Sequence[Entity], units: Sequence[DocumentUnit]
) -> list[LinkCandidate]:
    """A code entity's bare ``name`` appears verbatim in document text."""
    out: list[LinkCandidate] = []
    for entity in entities:
        pattern = _needle_pattern(entity.name)
        for unit in units:
            if _word_present(unit.text, pattern):
                out.append(
                    LinkCandidate(
                        entity_id=entity.id,
                        document_id=unit.document_id,
                        section_id=unit.id,
                        link_type=RelationshipType.MENTIONED_IN,
                        resolver="linker:exact_identifier",
                        confidence=Confidence.HIGH,
                        evidence=entity.name,
                    )
                )
    return out


def match_qualified_identifier(
    entities: Sequence[Entity], units: Sequence[DocumentUnit]
) -> list[LinkCandidate]:
    """A code entity's fully-qualified name appears verbatim in document
    text -- skipped when the qualified name has no "." (then it is
    identical to the bare name, already covered by
    ``match_exact_identifier``).
    """
    out: list[LinkCandidate] = []
    for entity in entities:
        if "." not in entity.qualified_name:
            continue
        pattern = _needle_pattern(entity.qualified_name)
        for unit in units:
            if _word_present(unit.text, pattern):
                out.append(
                    LinkCandidate(
                        entity_id=entity.id,
                        document_id=unit.document_id,
                        section_id=unit.id,
                        link_type=RelationshipType.DOCUMENTED_BY,
                        resolver="linker:qualified_identifier",
                        confidence=Confidence.HIGH,
                        evidence=entity.qualified_name,
                    )
                )
    return out


def match_alias(entities: Sequence[Entity], units: Sequence[DocumentUnit]) -> list[LinkCandidate]:
    """A modest, cheap alias: the qualified name's final two dotted
    segments (its class-and-member form, dropping the namespace prefix)
    -- e.g. "myproject.models.Dog.bark" also matches on "Dog.bark" alone.
    Skipped below ``_MIN_ALIAS_SEGMENTS`` segments, where the alias would
    just duplicate ``match_qualified_identifier``'s own match text.
    """
    out: list[LinkCandidate] = []
    for entity in entities:
        segments = entity.qualified_name.split(".")
        if len(segments) < _MIN_ALIAS_SEGMENTS:
            continue
        alias = ".".join(segments[-2:])
        pattern = _needle_pattern(alias)
        for unit in units:
            if _word_present(unit.text, pattern):
                out.append(
                    LinkCandidate(
                        entity_id=entity.id,
                        document_id=unit.document_id,
                        section_id=unit.id,
                        link_type=RelationshipType.MENTIONED_IN,
                        resolver="linker:alias",
                        confidence=Confidence.MEDIUM,
                        evidence=alias,
                    )
                )
    return out


def match_filename(
    namespace_entity: Entity | None,
    filename_candidates: Sequence[str],
    units: Sequence[DocumentUnit],
) -> list[LinkCandidate]:
    """A document mentions a source file's filename (with or without its
    extension). Attaches to that file's ``NAMESPACE`` entity specifically
    (the code processor's stand-in for "the file itself" -- Phase 2 has
    no first-class ``File`` entity), never to every entity in the file:
    fanning a filename mention out to every function/class the file
    happens to define would be noisy without adding real signal about
    which of them the document is actually about.
    """
    if namespace_entity is None:
        return []
    filename_patterns = [
        (filename, _needle_pattern(filename)) for filename in filename_candidates if filename
    ]
    out: list[LinkCandidate] = []
    for unit in units:
        for filename, pattern in filename_patterns:
            if _word_present(unit.text, pattern):
                out.append(
                    LinkCandidate(
                        entity_id=namespace_entity.id,
                        document_id=unit.document_id,
                        section_id=unit.id,
                        link_type=RelationshipType.MENTIONED_IN,
                        resolver="linker:filename",
                        confidence=Confidence.MEDIUM,
                        evidence=filename,
                    )
                )
                break
    return out


def match_route_heuristic(
    route_relationships: Sequence[Relationship], units: Sequence[DocumentUnit]
) -> list[LinkCandidate]:
    """Reuses Phase 2's ``framework_rules``-tagged HTTP endpoint findings
    (``relationships`` rows with ``target_symbol`` = "http_endpoint:
    METHOD:/path", see ``code/framework_rules.py``) against document text
    mentioning the same route path. A plain substring check is
    deliberately simpler than ``_word_present``'s boundary logic: a route
    path like "/orders/{id}" is already distinctive punctuation-wise, and
    this rule is HEURISTIC either way -- Phase 2 never claims more than a
    naming-convention guess for it, so neither does this.
    """
    out: list[LinkCandidate] = []
    for rel in route_relationships:
        if rel.target_symbol is None or not rel.target_symbol.startswith(_ROUTE_TARGET_PREFIX):
            continue
        remainder = rel.target_symbol[len(_ROUTE_TARGET_PREFIX) :]
        method, _, path = remainder.partition(":")
        if not path:
            continue
        for unit in units:
            if path in unit.text:
                out.append(
                    LinkCandidate(
                        entity_id=rel.source_entity_id,
                        document_id=unit.document_id,
                        section_id=unit.id,
                        link_type=RelationshipType.RELATED_TO,
                        resolver="linker:route_heuristic",
                        confidence=Confidence.HEURISTIC,
                        evidence=f"{method} {path}",
                    )
                )
    return out


def _filename_candidates(path: str) -> list[str]:
    p = Path(path)
    return [p.name] if p.name == p.stem else [p.name, p.stem]


def _group_entities_by_file(entities: Sequence[Entity]) -> dict[str, list[Entity]]:
    grouped: dict[str, list[Entity]] = {}
    for entity in entities:
        grouped.setdefault(entity.file_id, []).append(entity)
    return grouped


def _group_units_by_file(units: Sequence[DocumentUnit]) -> dict[str, list[DocumentUnit]]:
    grouped: dict[str, list[DocumentUnit]] = {}
    for unit in units:
        grouped.setdefault(unit.file_id, []).append(unit)
    return grouped


def _store(conn: sqlite3.Connection, candidates: Sequence[LinkCandidate]) -> int:
    now = _now()
    inserted = 0
    for c in candidates:
        link = CrossLink(
            id=uuid.uuid4().hex,
            link_type=c.link_type,
            entity_id=c.entity_id,
            document_id=c.document_id,
            section_id=c.section_id,
            resolver=c.resolver,
            confidence=c.confidence,
            evidence=c.evidence,
            created_at=now,
        )
        if links_repo.insert(conn, link):
            inserted += 1
    return inserted


def link_touched_files(
    conn: sqlite3.Connection,
    *,
    touched_code_file_ids: Sequence[str],
    touched_document_file_ids: Sequence[str],
) -> int:
    """Cross-domain linking pass for one project, run once per source
    after its per-file processor queue has fully drained (see
    ``cli/index.py`` -- this is deliberately not folded into
    ``IndexCoordinator``, which stays kind-agnostic and knows nothing
    about entities or documents).

    Scoped to files touched *this run*, matched in both directions
    against the rest of the project's full corpus. A link only ever
    depends on one code file and one document file, so a full-corpus
    recompute every run would be O(all entities x all document units) on
    every single ``ragmonk index`` regardless of how little changed --
    that does not scale. Incremental scoping keeps a run's linking cost
    proportional to what was actually (re)indexed this run, at one
    accepted cost: a link between two *already-indexed, unchanged* sides
    is only ever discovered once, the run where the later of the two
    sides first appeared -- if neither side is ever touched again, a
    match that a smarter matching rule *added in a future version* of
    this module would recognize is never retroactively found without a
    full reindex. Stale links (a touched file's previous generation) are
    already cleaned up before this runs, via ``entities_repo``/
    ``documents_repo``'s ``delete_by_file`` cascading into
    ``links_repo`` -- see those modules and ``core.models.CrossLink``.
    """
    if not touched_code_file_ids and not touched_document_file_ids:
        return 0

    all_entities = entities_repo.list_all(conn)
    all_units = documents_repo.list_all_units(conn)
    entities_by_file = _group_entities_by_file(all_entities)
    units_by_file = _group_units_by_file(all_units)
    route_relationships = relationships_repo.find_by_target_symbol_prefix(
        conn, _ROUTE_TARGET_PREFIX
    )

    inserted = 0

    # Indexing optimization plan, Phase P6: one batched query for every
    # touched code file's record, instead of the N individual
    # ``files_repo.get()`` round trips this loop used before -- each
    # touched file needs only its own path (for ``_filename_candidates``),
    # never another file's, so a single ``IN (...)`` covering exactly this
    # run's touched set is both correct and strictly cheaper than before.
    touched_code_files = files_repo.get_many(conn, touched_code_file_ids)

    for file_id in touched_code_file_ids:
        file_entities = entities_by_file.get(file_id, [])
        if not file_entities:
            continue
        file = touched_code_files.get(file_id)
        namespace_entity = next(
            (e for e in file_entities if e.kind is EntityType.NAMESPACE), None
        )
        filename_candidates = _filename_candidates(file.path) if file is not None else []
        entity_ids = {e.id for e in file_entities}
        file_routes = [r for r in route_relationships if r.source_entity_id in entity_ids]
        candidates = [
            *match_exact_identifier(file_entities, all_units),
            *match_qualified_identifier(file_entities, all_units),
            *match_alias(file_entities, all_units),
            *match_filename(namespace_entity, filename_candidates, all_units),
            *match_route_heuristic(file_routes, all_units),
        ]
        inserted += _store(conn, candidates)

    # Indexing optimization plan, Phase P6: this whole per-project-file
    # ``namespace_by_file`` map (used only by the document-side loop
    # below) is never needed at all on a pass that touched no document
    # files -- the overwhelmingly common case (an ordinary code edit).
    # Before this phase it was built unconditionally, which meant every
    # code-only indexing pass paid for one query per code file in the
    # *entire* project (a full N+1 round trip) for a result nothing then
    # read. Skipping it here, and batch-fetching the rest in one query
    # when it *is* needed, is this phase's other real SQLite-cost fix.
    namespace_by_file: dict[str, tuple[Entity | None, list[str]]] = {}
    if touched_document_file_ids:
        all_project_files = files_repo.get_many(conn, list(entities_by_file))
        for fid, ents in entities_by_file.items():
            namespace_entity = next((e for e in ents if e.kind is EntityType.NAMESPACE), None)
            other_file = all_project_files.get(fid)
            filename_candidates = (
                _filename_candidates(other_file.path) if other_file is not None else []
            )
            namespace_by_file[fid] = (namespace_entity, filename_candidates)

    for file_id in touched_document_file_ids:
        file_units = units_by_file.get(file_id, [])
        if not file_units:
            continue
        candidates = [
            *match_exact_identifier(all_entities, file_units),
            *match_qualified_identifier(all_entities, file_units),
            *match_alias(all_entities, file_units),
            *match_route_heuristic(route_relationships, file_units),
        ]
        for namespace_entity, filename_candidates in namespace_by_file.values():
            candidates.extend(match_filename(namespace_entity, filename_candidates, file_units))
        inserted += _store(conn, candidates)

    return inserted
