"""Computes and stores Phase 9 embeddings for touched files, mirroring
``knowledge/linker.py``'s ``link_touched_files`` shape and scoping:

Run once per source pass, after the per-file processor queue has fully
drained (``indexing/runner.py``), scoped to files touched *this run*
only -- embedding every entity/document section in the whole project on
every single ``ragpilot index`` would be O(all content) regardless of how
little changed, the exact cost problem ``link_touched_files`` already
rejected for the same reason. One accepted consequence, mirroring
``link_touched_files``'s own documented tradeoff: freshly turning on
``search.semantic`` for an *already-indexed, otherwise-unchanged* project
computes no embeddings until something actually touches those files
again -- ``ragpilot rebuild`` (every file becomes "new", hence touched)
is the documented way to force a full backfill, rather than this module
growing a second, whole-project code path.

Gated entirely behind ``search.semantic`` by its one caller
(``indexing/runner.py``): enabling that config is the single switch that
turns on both computing embeddings here and using them in
``retrieval/semantic.py``, never one without the other.

Search Quality Improvement Plan, Phase 12: ``touched_*_file_ids`` is no
longer only "files the processor queue actually reprocessed this run" --
``indexing/runner.py`` unions in ``embeddings_stale_*_file_ids`` too, a
file whose content never changed but whose stored embedding version stamp
(``files.embedding_model_id``/``embedding_text_version``) no longer
matches current code (``indexing/incremental.decide_reprocessing``). Such
a file is embedded here exactly like a freshly-touched one -- this
function itself does not distinguish the two -- while ``document_sections``
/``entities``/FTS are never rebuilt for it, since nothing about how they
were derived changed. This does not remove the tradeoff two paragraphs up
(a file whose embedding stamp was never populated at all -- e.g. any file
indexed while ``search.semantic`` was off -- stays untracked, not stale,
until something else touches it).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from ragpilot.core.models import EmbeddingSubjectType, Entity
from ragpilot.retrieval import embedder
from ragpilot.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragpilot.telemetry.logging import get_logger, log_event

_logger = get_logger("embeddings")

# Search Quality Improvement Plan, Phase 12: the version of ``_entity_text``
# below -- this module's own text-assembly step for a code entity, the
# code-kind counterpart of ``documents/chunker.py``'s
# ``EMBEDDING_TEXT_VERSION`` for a document chunk's ``contextual_text``.
# Not imported from ``documents/chunker.py`` for a document-kind file's
# stamp either, for the same reason that module is imported lazily below:
# ``documents/chunker.py`` transitively pulls in ``docling_core`` (via
# ``documents/normalizer.py``), a cost this module -- imported
# unconditionally by ``indexing/runner.py`` -- must not impose on every
# ``ragpilot index`` run regardless of whether documents are even enabled.
CODE_EMBEDDING_TEXT_VERSION = "1"


def _entity_text(entity: Entity) -> str:
    return entity.signature or entity.qualified_name


def embed_touched_files(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    touched_code_file_ids: Sequence[str],
    touched_document_file_ids: Sequence[str],
) -> int:
    """Embeds every non-empty-text entity/document-section belonging to a
    touched file, replacing that file's previous embeddings generation
    (any model) in the same caller-held transaction -- run inside the
    same ``with transaction(conn):`` block as the rest of one source
    pass's writes, matching ``link_touched_files``.

    Returns the number of vectors stored. Never raises for a missing or
    unloadable embedding model: it logs and returns ``0`` instead, so
    ``search.semantic`` being on can never turn an ordinary ``ragpilot
    index`` run into a hard failure -- semantic search just stays
    unavailable until the model loads (see ``retrieval/semantic.py``).
    """
    if not touched_code_file_ids and not touched_document_file_ids:
        return 0

    subjects: list[tuple[EmbeddingSubjectType, str, str, str]] = []
    for file_id in touched_code_file_ids:
        for entity in entities_repo.list_by_file(conn, file_id):
            text = _entity_text(entity)
            if text.strip():
                subjects.append((EmbeddingSubjectType.ENTITY, entity.id, file_id, text))
    for file_id in touched_document_file_ids:
        for unit in documents_repo.list_units_by_file(conn, file_id):
            # `embedding_text` (search-quality plan Phase 3) is the
            # document-title + heading-path-contextualized rendering of
            # `text` computed at chunk time (`chunker.Chunk.
            # contextual_text`) -- embedding that instead of the raw,
            # isolated section text is what actually improves natural-
            # language/semantic retrieval (see `documents/chunker.py`'s
            # module docstring). Falls back to `unit.text` only for a row
            # written before this field existed (pre-migration, never
            # reindexed since) or a direct-insert caller that left it
            # unset -- never a hard failure either way.
            text = unit.embedding_text or unit.text
            if text.strip():
                subjects.append((EmbeddingSubjectType.DOCUMENT_SECTION, unit.id, file_id, text))

    if not subjects:
        return 0

    try:
        vectors = embedder.embed_texts([s[3] for s in subjects])
    except embedder.EmbeddingModelUnavailableError as exc:
        log_event(
            _logger,
            "embedding_model_unavailable",
            level=logging.WARNING,
            source_id=source_id,
            error=str(exc),
        )
        return 0

    # Lazy: documents/chunker.py transitively imports docling_core -- see
    # CODE_EMBEDDING_TEXT_VERSION's docstring above for why that cost must
    # only be paid once embeddings are actually about to be computed, not
    # at this module's own import time.
    from ragpilot.documents.chunker import (
        EMBEDDING_TEXT_VERSION as _document_embedding_text_version,
    )

    now = datetime.now(UTC).isoformat()
    document_touched = set(touched_document_file_ids)
    touched_files = set(touched_code_file_ids) | document_touched
    for file_id in touched_files:
        embeddings_repo.delete_by_file(conn, file_id)
        # Mirrors embeddings_repo.delete_by_file: vector_items is the ANN
        # index's own id-mapping table (blueprint section 13), regenerated
        # in lockstep with embeddings so the two never drift apart.
        vector_items_repo.delete_by_file(conn, file_id)
        # Search Quality Improvement Plan, Phase 12: stamp this file's
        # embedding reuse identity now that its vectors are genuinely
        # about to be (re)computed below -- never speculatively before
        # this point, so a failed/skipped embedding step (see the
        # EmbeddingModelUnavailableError branch above) never claims a
        # rebuild that didn't happen.
        files_repo.update_embedding_version(
            conn,
            file_id,
            embedding_model_id=embedder.EMBEDDING_MODEL_ID,
            embedding_text_version=(
                _document_embedding_text_version
                if file_id in document_touched
                else CODE_EMBEDDING_TEXT_VERSION
            ),
            updated_at=now,
        )

    for (subject_type, subject_id, file_id, _text), vector in zip(subjects, vectors, strict=True):
        embeddings_repo.insert(
            conn,
            subject_type=subject_type,
            subject_id=subject_id,
            file_id=file_id,
            source_id=source_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
            vector=vector,
        )
        vector_items_repo.insert(
            conn,
            subject_type=subject_type.value,
            subject_id=subject_id,
            file_id=file_id,
            source_id=source_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
        )

    log_event(
        _logger,
        "embeddings_computed",
        source_id=source_id,
        count=len(subjects),
    )
    return len(subjects)
