"""Computes and stores Phase 9 embeddings for touched files, mirroring
``knowledge/linker.py``'s ``link_touched_files`` shape and scoping:

Run once per source pass, after the per-file processor queue has fully
drained (``indexing/runner.py``), scoped to files touched *this run*
only -- embedding every entity/document section in the whole project on
every single ``ragmonk index`` would be O(all content) regardless of how
little changed, the exact cost problem ``link_touched_files`` already
rejected for the same reason. One accepted consequence, mirroring
``link_touched_files``'s own documented tradeoff: freshly turning on
``search.semantic`` for an *already-indexed, otherwise-unchanged* project
computes no embeddings until something actually touches those files
again -- ``ragmonk rebuild`` (every file becomes "new", hence touched)
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
from dataclasses import dataclass
from datetime import UTC, datetime

from ragmonk.core.models import EmbeddingSubjectType, Entity
from ragmonk.retrieval import embedder
from ragmonk.storage.repositories import (
    documents_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragmonk.telemetry.logging import get_logger, log_event

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
# ``ragmonk index`` run regardless of whether documents are even enabled.
CODE_EMBEDDING_TEXT_VERSION = "1"


def _entity_text(entity: Entity) -> str:
    return entity.signature or entity.qualified_name


@dataclass(frozen=True)
class PreparedEmbeddings:
    """The read-only, model-inference half of one embeddings backfill
    (indexing optimization plan, Phase P5): every subject to (re)embed
    plus its already-computed vector, with nothing here touching the
    database. Handing this to ``publish_embeddings`` is what lets a
    caller keep model inference -- the genuinely slow, ``BEGIN
    IMMEDIATE``-lock-free part -- entirely outside its write transaction,
    instead of the pre-P5 shape where ``embed_touched_files`` ran
    inference *and* writes as one call already inside the caller's
    transaction (see ``indexing/runner.py``'s ``run_source_pass``).
    """

    source_id: str
    subjects: list[tuple[EmbeddingSubjectType, str, str, str]]
    vectors: list[list[float]]
    touched_code_file_ids: frozenset[str]
    touched_document_file_ids: frozenset[str]


def prepare_embeddings(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    touched_code_file_ids: Sequence[str],
    touched_document_file_ids: Sequence[str],
    batch_size: int | None = None,
) -> PreparedEmbeddings | None:
    """Gathers every non-empty-text entity/document-section belonging to
    a touched file and computes its vector -- pure reads plus model
    inference, no writes. Call this *before* opening a write transaction;
    pass the result to ``publish_embeddings`` inside one.

    Returns ``None`` when there is nothing to embed, or when the model
    cannot be loaded (logged, never raised -- ``search.semantic`` being on
    must never turn an ordinary ``ragmonk index`` run into a hard
    failure; semantic search just stays unavailable until the model
    loads, see ``retrieval/semantic.py``).
    """
    if not touched_code_file_ids and not touched_document_file_ids:
        return None

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
        return None

    # Phase P5, finding "reuse vectors for identical embedding text": two
    # subjects with the exact same text (e.g. two overloads sharing a
    # signature, or a document chunk duplicated across sections) need the
    # model run on it only once -- dedup within this batch before calling
    # the model, then fan the one resulting vector back out to every
    # subject that shared the text. Scoped to this single batch (not
    # persisted/reused across files or projects) so this can never leak a
    # vector between projects or permission boundaries the way a
    # cross-project cache would.
    unique_texts: dict[str, int] = {}
    for _subject_type, _subject_id, _file_id, text in subjects:
        if text not in unique_texts:
            unique_texts[text] = len(unique_texts)
    ordered_texts = list(unique_texts)

    try:
        unique_vectors = embedder.embed_texts(ordered_texts, batch_size=batch_size)
    except embedder.EmbeddingModelUnavailableError as exc:
        log_event(
            _logger,
            "embedding_model_unavailable",
            level=logging.WARNING,
            source_id=source_id,
            error=str(exc),
        )
        return None

    reused = len(subjects) - len(ordered_texts)
    if reused:
        log_event(
            _logger,
            "embeddings_deduplicated",
            source_id=source_id,
            unique=len(ordered_texts),
            reused=reused,
        )

    vectors = [unique_vectors[unique_texts[s[3]]] for s in subjects]

    return PreparedEmbeddings(
        source_id=source_id,
        subjects=subjects,
        vectors=vectors,
        touched_code_file_ids=frozenset(touched_code_file_ids),
        touched_document_file_ids=frozenset(touched_document_file_ids),
    )


def publish_embeddings(conn: sqlite3.Connection, prepared: PreparedEmbeddings) -> int:
    """The write half: replaces every touched file's previous embeddings
    generation (any model) with ``prepared``'s already-computed vectors,
    and stamps each file's embedding reuse identity. Deliberately no
    model inference here -- run this *inside* the caller's ``with
    transaction(conn):`` block (mirroring ``link_touched_files``), now
    short enough to hold ``BEGIN IMMEDIATE`` for only as long as the
    writes themselves take, not however long the model took to run.

    Returns the number of vectors stored.
    """
    # Lazy: documents/chunker.py transitively imports docling_core -- see
    # CODE_EMBEDDING_TEXT_VERSION's docstring above for why that cost must
    # only be paid once embeddings are actually about to be committed, not
    # at this module's own import time.
    from ragmonk.documents.chunker import (
        EMBEDDING_TEXT_VERSION as _document_embedding_text_version,
    )

    now = datetime.now(UTC).isoformat()
    document_touched = prepared.touched_document_file_ids
    touched_files = prepared.touched_code_file_ids | document_touched
    for file_id in touched_files:
        embeddings_repo.delete_by_file(conn, file_id)
        # Mirrors embeddings_repo.delete_by_file: vector_items is the ANN
        # index's own id-mapping table (blueprint section 13), regenerated
        # in lockstep with embeddings so the two never drift apart.
        vector_items_repo.delete_by_file(conn, file_id)
        # Search Quality Improvement Plan, Phase 12 (extended by Phase
        # P5's prepare/publish split): stamp this file's embedding reuse
        # identity only here, inside the same transaction as the vectors
        # themselves -- never speculatively before ``prepare_embeddings``
        # succeeded, so a failed/unavailable model run (which returns
        # ``None`` and never reaches this function at all) never claims a
        # rebuild that didn't happen, and a mid-transaction failure here
        # rolls the stamp back right along with the rows it describes.
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

    for (subject_type, subject_id, file_id, _text), vector in zip(
        prepared.subjects, prepared.vectors, strict=True
    ):
        embeddings_repo.insert(
            conn,
            subject_type=subject_type,
            subject_id=subject_id,
            file_id=file_id,
            source_id=prepared.source_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
            vector=vector,
        )
        vector_items_repo.insert(
            conn,
            subject_type=subject_type.value,
            subject_id=subject_id,
            file_id=file_id,
            source_id=prepared.source_id,
            model_id=embedder.EMBEDDING_MODEL_ID,
        )

    log_event(
        _logger,
        "embeddings_computed",
        source_id=prepared.source_id,
        count=len(prepared.subjects),
    )
    return len(prepared.subjects)


def embed_touched_files(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    touched_code_file_ids: Sequence[str],
    touched_document_file_ids: Sequence[str],
) -> int:
    """Convenience wrapper combining ``prepare_embeddings`` +
    ``publish_embeddings`` as a single call, preserving this module's
    pre-P5 signature and behavior for any caller that doesn't need model
    inference kept outside its transaction (tests, one-off scripts).
    ``indexing/runner.py``'s real source-pass path calls the two halves
    separately instead -- see ``run_source_pass``.
    """
    prepared = prepare_embeddings(
        conn,
        source_id=source_id,
        touched_code_file_ids=touched_code_file_ids,
        touched_document_file_ids=touched_document_file_ids,
    )
    if prepared is None:
        return 0
    return publish_embeddings(conn, prepared)
