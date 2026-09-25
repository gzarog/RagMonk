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
    embedding_cache_repo,
    embeddings_repo,
    entities_repo,
    files_repo,
    vector_items_repo,
)
from ragmonk.telemetry.logging import get_logger, log_event
from ragmonk.tokenization import model_identity

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
    # Indexing optimization plan V2, Phase P3: every distinct text this
    # batch embedded (whether reused from the persistent cache or freshly
    # computed), ready for ``publish_embeddings`` to upsert into
    # ``embedding_cache`` -- (text_hash, embedding_text_version, vector).
    # Upserting a text that was already cached (a cache hit) is a
    # harmless no-op write, kept for simplicity rather than tracking
    # hit/miss separately through this struct.
    cache_entries: list[tuple[str, str, list[float]]]
    # Telemetry only (V2 Phase P5 wiring point): how many of this batch's
    # *unique* texts were served from the persistent cache without
    # calling the model at all.
    cache_reused: int


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

    # Indexing optimization plan V2, Phase P4 (measured): both loops
    # below used to call list_by_file/list_units_by_file once *per
    # touched file* -- N round trips for N touched files. Measured on a
    # synthetic 300-file batch: 300 individual SELECTs each, confirmed by
    # SQLite statement counting (see this phase's commit message for the
    # full before/after numbers and the EXPLAIN QUERY PLAN check that
    # ruled out an index -- both were already simple, single-table
    # equality-filtered scans; the fix is call count, not query plan).
    # entities_repo.list_by_files/documents_repo.list_units_by_files
    # (mirroring files_repo.get_many's identical Phase P6 precedent) get
    # every touched file's rows in one query each, then this function
    # groups them back out per file itself.
    subjects: list[tuple[EmbeddingSubjectType, str, str, str]] = []
    entities_by_file: dict[str, list[Entity]] = {}
    for entity in entities_repo.list_by_files(conn, list(touched_code_file_ids)):
        entities_by_file.setdefault(entity.file_id, []).append(entity)
    for file_id in touched_code_file_ids:
        for entity in entities_by_file.get(file_id, []):
            text = _entity_text(entity)
            if text.strip():
                subjects.append((EmbeddingSubjectType.ENTITY, entity.id, file_id, text))

    units_by_file: dict[str, list[documents_repo.DocumentUnit]] = {}
    for unit in documents_repo.list_units_by_files(conn, list(touched_document_file_ids)):
        units_by_file.setdefault(unit.file_id, []).append(unit)
    for file_id in touched_document_file_ids:
        for unit in units_by_file.get(file_id, []):
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

    # Indexing optimization plan V2, Phase P3: which text-assembly
    # version produced each unique text -- CODE_EMBEDDING_TEXT_VERSION
    # for an entity, documents/chunker.EMBEDDING_TEXT_VERSION for a
    # document section -- keyed by the *first* subject that produced each
    # text, mirroring the batch-level dedup immediately above (which
    # already merges same-text subjects across kinds into one shared
    # vector regardless of type; this cache never introduces a finer
    # distinction than that pre-existing behavior already makes). The
    # document chunker import stays lazy, and only actually happens when
    # this batch has a document subject at all -- see
    # CODE_EMBEDDING_TEXT_VERSION's own docstring for why that cost must
    # be conditional.
    text_versions: dict[str, str] = {}
    document_text_version: str | None = None
    for subject_type, _subject_id, _file_id, text in subjects:
        if text in text_versions:
            continue
        if subject_type is EmbeddingSubjectType.DOCUMENT_SECTION:
            if document_text_version is None:
                from ragmonk.documents.chunker import (
                    EMBEDDING_TEXT_VERSION as _document_embedding_text_version,
                )

                document_text_version = _document_embedding_text_version
            text_versions[text] = document_text_version
        else:
            text_versions[text] = CODE_EMBEDDING_TEXT_VERSION

    # Indexing optimization plan V2, Phase P3: reuse a persistent,
    # project-local vector for any of this batch's unique texts whose
    # full identity (exact text + model + preprocessing + embedding-text
    # version) already has a cached row, instead of paying for model
    # inference on it again -- this project's own single-project-per-
    # database layout (``core/lifecycle.AppContext.project_conn``) is
    # what makes "project-local" free: this connection can only ever read
    # this one project's ``embedding_cache`` table. Layered *on top of*
    # the within-batch dedup immediately above, never replacing it -- a
    # text still costs at most one cache lookup and, on a miss, one model
    # call, regardless of how many subjects in this batch share it.
    preprocessing_version = model_identity.preprocessing_fingerprint()
    cached_vectors: dict[str, list[float]] = {}
    for text in ordered_texts:
        cached = embedding_cache_repo.get(
            conn,
            embedding_cache_repo.text_hash(text),
            model_id=embedder.EMBEDDING_MODEL_ID,
            preprocessing_version=preprocessing_version,
            embedding_text_version=text_versions[text],
        )
        if cached is not None:
            cached_vectors[text] = cached

    texts_to_embed = [text for text in ordered_texts if text not in cached_vectors]
    if texts_to_embed:
        try:
            freshly_computed = embedder.embed_texts(texts_to_embed, batch_size=batch_size)
        except embedder.EmbeddingModelUnavailableError as exc:
            log_event(
                _logger,
                "embedding_model_unavailable",
                level=logging.WARNING,
                source_id=source_id,
                error=str(exc),
            )
            return None
    else:
        freshly_computed = []
    computed_by_text = dict(zip(texts_to_embed, freshly_computed, strict=True))

    unique_vectors = [
        cached_vectors[text] if text in cached_vectors else computed_by_text[text]
        for text in ordered_texts
    ]

    reused = len(subjects) - len(ordered_texts)
    if reused:
        log_event(
            _logger,
            "embeddings_deduplicated",
            source_id=source_id,
            unique=len(ordered_texts),
            reused=reused,
        )
    if cached_vectors:
        log_event(
            _logger,
            "embeddings_cache_reused",
            source_id=source_id,
            reused=len(cached_vectors),
            computed=len(texts_to_embed),
        )

    vectors = [unique_vectors[unique_texts[s[3]]] for s in subjects]
    cache_entries = [
        (
            embedding_cache_repo.text_hash(text),
            text_versions[text],
            unique_vectors[unique_texts[text]],
        )
        for text in ordered_texts
    ]

    return PreparedEmbeddings(
        source_id=source_id,
        subjects=subjects,
        vectors=vectors,
        touched_code_file_ids=frozenset(touched_code_file_ids),
        touched_document_file_ids=frozenset(touched_document_file_ids),
        cache_entries=cache_entries,
        cache_reused=len(cached_vectors),
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
    code_touched = prepared.touched_code_file_ids
    touched_files = code_touched | document_touched

    # Indexing optimization plan V2, Phase P4 (measured): the delete and
    # version-stamp steps below used to run once *per touched file* --
    # measured (see this phase's commit message) at 600 statements for a
    # 300-file batch (300 deletes across two tables + 300 UPDATEs).
    # Already inside one caller-held transaction covering the whole
    # batch (unchanged from Phase P5), so folding these into a handful
    # of multi-row statements changes only statement count, never
    # failure semantics -- the whole batch still commits or rolls back
    # together exactly as before.
    embeddings_repo.delete_by_files(conn, list(touched_files))
    # Mirrors embeddings_repo.delete_by_files: vector_items is the ANN
    # index's own id-mapping table (blueprint section 13), regenerated
    # in lockstep with embeddings so the two never drift apart.
    vector_items_repo.delete_by_files(conn, list(touched_files))
    # Search Quality Improvement Plan, Phase 12 (extended by Phase P5's
    # prepare/publish split): stamp every touched file's embedding reuse
    # identity only here, inside the same transaction as the vectors
    # themselves -- never speculatively before ``prepare_embeddings``
    # succeeded, so a failed/unavailable model run (which returns
    # ``None`` and never reaches this function at all) never claims a
    # rebuild that didn't happen, and a mid-transaction failure here
    # rolls the stamp back right along with the rows it describes. Split
    # into (at most) two batched calls -- one per embedding_text_version
    # group -- rather than N per-file ones, mirroring the deletes above.
    files_repo.update_embedding_version_many(
        conn,
        list(code_touched),
        embedding_model_id=embedder.EMBEDDING_MODEL_ID,
        embedding_text_version=CODE_EMBEDDING_TEXT_VERSION,
        updated_at=now,
    )
    files_repo.update_embedding_version_many(
        conn,
        list(document_touched),
        embedding_model_id=embedder.EMBEDDING_MODEL_ID,
        embedding_text_version=_document_embedding_text_version,
        updated_at=now,
    )

    # Phase P4 measured and *rejected* executemany here (see this
    # phase's commit message): per-row INSERT via a Python loop vs.
    # ``executemany`` showed no reliable wall-time difference for this
    # table shape (~2000-row synthetic batch, both ~20-30ms, noise-level
    # apart) -- unlike the DELETE/UPDATE batching above, an
    # ``executemany`` INSERT still executes one prepared-statement run
    # per row under the hood (confirmed via ``sqlite3.Connection.
    # set_trace_callback`` statement counting: identical statement count
    # to the loop, not reduced), so there was no real win to keep here.
    # Left as the original per-subject loop.
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

    # Indexing optimization plan V2, Phase P3: upsert every unique text
    # this batch embedded (whether served from the cache or freshly
    # computed -- see PreparedEmbeddings.cache_entries's docstring for why
    # re-upserting a hit is a harmless no-op) into the persistent,
    # project-local cache, in the *same* transaction as the
    # embeddings/vector_items rows just written above. This is what keeps
    # the cache consistent with the existing SQLite/ANN convergence
    # mechanism: an interrupted publish (an exception before this
    # transaction's COMMIT) rolls the cache writes back right along with
    # the vectors they were derived alongside, so a cache row can never
    # describe a vector that was never actually published, and the ANN
    # sync that runs after a successful commit (``indexing/runner.py``'s
    # ``run_source_pass``, unchanged by this phase) still has SQLite as
    # its sole, self-healing source of truth either way. Left as a
    # per-entry loop -- same measured-and-rejected executemany finding
    # as the inserts immediately above applies here too.
    preprocessing_version = model_identity.preprocessing_fingerprint()
    for hashed_text, embedding_text_version, vector in prepared.cache_entries:
        embedding_cache_repo.put(
            conn,
            hashed_text,
            model_id=embedder.EMBEDDING_MODEL_ID,
            preprocessing_version=preprocessing_version,
            embedding_text_version=embedding_text_version,
            vector=vector,
            created_at=now,
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
