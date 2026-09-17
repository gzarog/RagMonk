"""Resolves raw extracted references (call/inherit/import targets, given
only as text) into confidence-scored links to known entities.

Confidence rule -- the non-obvious design decision this module encodes:

  EXACT     A single, unambiguous match within the SAME file as the
            reference. Same-file resolution never depends on guessing
            across a project's naming conventions, so an unambiguous hit
            here is as certain as this generic (no type-inference)
            resolver gets.
  HIGH      Either (a) more than one same-file candidate shares the bare
            name (still same file, so still trustworthy, just not
            provably unambiguous), or (b) a different file in the same
            project has an entity whose *qualified* name exactly equals
            the reference text -- an exact qualified-name match is a
            strong signal even across files.
  MEDIUM    Resolution fell back to a bare name only (no qualification),
            possibly matching more than one candidate project-wide, or
            found no candidate at all. Per the blueprint, an unresolved
            reference is still recorded -- pointing at ``target_symbol``
            rather than an entity id -- rather than being dropped, and
            MEDIUM is the tier for that "low confidence, kept anyway"
            case (see relationships_repo.incoming_by_symbol).
  HEURISTIC Reserved for framework_rules.py. Never assigned here: nothing
            in this module goes beyond a generic name/qualified-name
            match, so nothing it produces is a framework heuristic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ragmonk.core.models import Confidence, Entity


@dataclass(frozen=True)
class ResolvedTarget:
    entity_id: str | None
    symbol: str
    confidence: Confidence
    resolver: str


def resolve_reference(
    text: str,
    *,
    same_file_entities: Sequence[Entity],
    qualified_name_lookup: Callable[[str], Sequence[Entity]],
    name_lookup: Callable[[str], Sequence[Entity]],
) -> ResolvedTarget:
    """Resolves ``text`` (a call target, base-type name, or import module).

    ``text`` may be a bare name ("bark"), a receiver-prefixed expression
    ("self.bark", "d.bark") or an actual dotted qualified name
    ("myproject.utils"). The lookups are dependency-injected so this stays
    pure/DB-free and unit-testable; ``code/processor.py`` wires them to
    ``entities_repo``.
    """
    bare_name = text.rsplit(".", 1)[-1] if text else text
    if not bare_name:
        return ResolvedTarget(None, text, Confidence.MEDIUM, "unresolved")

    same_file_by_qn = [e for e in same_file_entities if e.qualified_name == text]
    if len(same_file_by_qn) == 1:
        return ResolvedTarget(same_file_by_qn[0].id, text, Confidence.EXACT, "same_file_qualified")

    same_file_by_name = sorted(
        (e for e in same_file_entities if e.name == bare_name),
        key=lambda e: e.qualified_name,
    )
    if len(same_file_by_name) == 1:
        return ResolvedTarget(same_file_by_name[0].id, text, Confidence.EXACT, "same_file_name")
    if len(same_file_by_name) > 1:
        return ResolvedTarget(
            same_file_by_name[0].id, text, Confidence.HIGH, "same_file_name_ambiguous"
        )

    cross_by_qn = sorted(qualified_name_lookup(text), key=lambda e: (e.file_id, e.start_line))
    if cross_by_qn:
        resolver = (
            "cross_file_qualified" if len(cross_by_qn) == 1 else "cross_file_qualified_ambiguous"
        )
        return ResolvedTarget(cross_by_qn[0].id, text, Confidence.HIGH, resolver)

    cross_by_name = sorted(name_lookup(bare_name), key=lambda e: (e.qualified_name, e.file_id))
    if cross_by_name:
        return ResolvedTarget(cross_by_name[0].id, bare_name, Confidence.MEDIUM, "name_only")

    return ResolvedTarget(None, bare_name, Confidence.MEDIUM, "unresolved")
