"""Decides new/changed/unchanged/deleted by comparing a scan against the
file records already stored for a source.

Mtime+size is checked first (the "fast-skip"); the content hash is only
computed when that stat comparison can't already prove the file is
unchanged, and even then a hash match still counts as unchanged (handles
a touch that bumps mtime without changing content).

Search Quality Improvement Plan, Phase 12: content-hash equality alone is
no longer the whole story for a file classified ``UNCHANGED`` here -- the
*code* that turns content into chunks/FTS/embeddings can itself change
(a chunker rewrite, a new embedding model) without the file's bytes
changing at all, which would otherwise leave stale derived rows silently
served forever. ``VersionStamp``/``decide_reprocessing`` below add that
second axis on top of this module's existing content classification --
see ``indexing/coordinator.py`` for where the two are combined.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ragmonk.core.models import FileRecord, ScannedFile
from ragmonk.sources.fingerprint import stat_unchanged


class ChangeType(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


def classify_change(
    existing: FileRecord | None,
    size: int,
    mtime: float,
    compute_hash: Callable[[], str],
) -> tuple[ChangeType, str]:
    if existing is None:
        return ChangeType.NEW, compute_hash()
    if stat_unchanged(existing.size, existing.mtime, size, mtime):
        return ChangeType.UNCHANGED, existing.content_hash or compute_hash()
    new_hash = compute_hash()
    if existing.content_hash is not None and new_hash == existing.content_hash:
        return ChangeType.UNCHANGED, new_hash
    return ChangeType.CHANGED, new_hash


def find_deleted(
    existing_by_path: dict[str, FileRecord], scanned: list[ScannedFile]
) -> list[FileRecord]:
    seen = {sf.path for sf in scanned}
    return [record for path, record in existing_by_path.items() if path not in seen]


@dataclass(frozen=True)
class VersionStamp:
    """One side of the composite reuse identity Search Quality
    Improvement Plan, Phase 12 compares -- either what a file's stored
    ``document_sections``/``entities``/``embeddings`` rows were actually
    produced by (read off ``FileRecord``'s own ``parser_version``/
    ``chunker_version``/``embedding_model_id``/``embedding_text_version``
    columns), or what the current code would produce right now (read off
    a registered processor's version-provider callable, e.g.
    ``documents/pipeline.py``'s ``document_version_stamp``).

    Every field is ``str | None`` on *both* sides: ``None`` on the stored
    side means "never stamped" (a file indexed before this tracking
    existed, or by a processor with no version provider at all);
    ``None`` on the current side means "this kind has no such axis" (see
    ``decide_reprocessing``'s docstring for why neither case forces a
    rebuild by itself).
    """

    parser_version: str | None
    chunker_version: str | None
    embedding_model_id: str | None
    embedding_text_version: str | None


class ReprocessDecision(StrEnum):
    NONE = "none"
    FULL = "full"
    EMBEDDINGS_ONLY = "embeddings_only"


def _stale(existing_value: str | None, current_value: str | None) -> bool:
    """A stored value only counts as stale when it is actually known and
    disagrees with the current one -- ``None`` on either side (see
    ``VersionStamp``'s docstring) is never itself grounds for a rebuild.
    This is what keeps adding a new version axis (a migration, a fresh
    ``VersionStamp`` field) from forcing every already-indexed file to
    reprocess the moment the tracking code ships: every existing row
    starts out ``NULL`` for a brand-new column and simply stays
    untracked-but-not-stale until it is next reprocessed for an
    unrelated reason.
    """
    return existing_value is not None and existing_value != current_value


def decide_reprocessing(existing: VersionStamp, current: VersionStamp) -> ReprocessDecision:
    """Given a file whose *content* is unchanged (``ChangeType.UNCHANGED``
    already established by ``classify_change``), decides whether its
    derived rows still need rebuilding purely because the code that
    derives them has moved on:

    - ``parser_version``/``chunker_version`` stale -> ``FULL``: the parsed
      document or its chunk boundaries could now differ, so chunks, FTS,
      *and* the embeddings derived from them (chunk boundaries changed
      under them) all need rebuilding -- the same work a genuinely
      ``CHANGED`` file gets.
    - Otherwise, ``embedding_model_id``/``embedding_text_version`` stale
      -> ``EMBEDDINGS_ONLY``: chunks/FTS are still exactly right, only the
      vectors derived from them could differ -- a narrower, cheaper
      rebuild that never touches ``document_sections``/``document_fts``.
    - Neither -> ``NONE``: today's fast-path behavior, unchanged.

    Order matters: a ``FULL`` rebuild already implies re-embedding (new
    chunks need new vectors regardless of the embedding stamps), so the
    parser/chunker check is made first and short-circuits the narrower
    one.
    """
    if _stale(existing.parser_version, current.parser_version) or _stale(
        existing.chunker_version, current.chunker_version
    ):
        return ReprocessDecision.FULL
    if _stale(existing.embedding_model_id, current.embedding_model_id) or _stale(
        existing.embedding_text_version, current.embedding_text_version
    ):
        return ReprocessDecision.EMBEDDINGS_ONLY
    return ReprocessDecision.NONE
