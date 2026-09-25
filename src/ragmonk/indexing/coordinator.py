"""Orchestrates scan -> classify -> enqueue -> process for one source.

The processor registry is the extension point later phases hook into:
Phase 2/3 register real code/document processors keyed by ``FileKind``;
Phase 1 ships only the default "raw" processor, which just records file
metadata and marks the file INDEXED (or SKIPPED_LIMIT if oversized).

Search Quality Improvement Plan, Phase 12: a scanned file no longer only
gets NEW/CHANGED/UNCHANGED from content-hash comparison alone (see
``indexing/incremental.py``). ``_reconcile_renames`` first folds a moved/
renamed file back onto its existing row (by content hash) so it reads as
UNCHANGED rather than delete+recreate-as-new; then, for a file that is
genuinely UNCHANGED, ``_version_reprocess_decision`` checks whether the
*code* that derives its chunks/embeddings has moved on since it was last
processed, upgrading it to a full reprocess or a narrower embeddings-only
one as needed (``ReprocessDecision``).
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ragmonk.core.config import ChunkingConfig, RagMonkConfig
from ragmonk.core.models import FileKind, FileRecord, FileStatus, ScannedFile
from ragmonk.indexing import retry
from ragmonk.indexing.incremental import (
    ChangeType,
    ReprocessDecision,
    VersionStamp,
    classify_change,
    decide_reprocessing,
    find_deleted,
)
from ragmonk.security.path_guard import PathGuard
from ragmonk.sources import detector
from ragmonk.sources.fingerprint import hash_file
from ragmonk.sources.ignore import IgnoreMatcher
from ragmonk.sources.scanner import ScanOutcome, check_root_accessible, scan
from ragmonk.storage.repositories import errors_repo, files_repo, jobs_repo
from ragmonk.telemetry.logging import get_logger, log_event

_logger = get_logger("indexer")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProcessorContext:
    path: Path
    size: int
    kind: FileKind
    max_size_bytes: int
    # Populated so a processor that derives entities (Phase 2's
    # CodeProcessor) can write them atomically alongside this run's file
    # record, without the coordinator's claim/retry/backoff loop above
    # needing to know anything about entities/relationships. Phase 1's
    # raw_processor ignores all four.
    conn: sqlite3.Connection | None = None
    source_id: str | None = None
    file_id: str | None = None
    source_root: Path | None = None
    # ``config.documents.max_pages`` -- Phase 3's DocumentProcessor checks
    # this itself (cheaply, before any heavy conversion) rather than the
    # coordinator pre-filtering by page count, since page count is not
    # knowable until a processor has looked at the file.
    max_document_pages: int | None = None
    # ``config.documents.chunking`` -- Phase 3's DocumentProcessor threads
    # this straight through to ``chunker.chunk_document`` rather than the
    # coordinator knowing anything about chunk boundaries itself, matching
    # ``max_document_pages`` immediately above. ``None`` (the default a
    # processor context built outside this coordinator, e.g. in a test,
    # would have) means "use ``ChunkingConfig()``'s own defaults" -- see
    # ``documents/pipeline.py``.
    chunking: ChunkingConfig | None = None
    # ``config.documents.ocr`` -- Phase 5's OCR fallback mode ("off" |
    # "auto" | "always"), threaded straight through to
    # ``docling_adapter.convert`` exactly like ``chunking``/
    # ``max_document_pages`` above. ``None`` (the default a
    # coordinator-external ``ProcessorContext``, e.g. a test, would have)
    # means "no OCR" -- ``documents/pipeline.py`` falls back to ``"off"``,
    # preserving every pre-Phase-5 caller's exact behavior rather than
    # silently opting them into OCR.
    ocr: str | None = None
    # ``config.documents.image_ocr`` -- Search Quality Improvement Plan,
    # Phase 10's opt-in switch for real text extraction from a raw image
    # (see ``core/config.py``'s ``DocumentsConfig.image_ocr`` and
    # ``documents/pipeline.py``). ``None`` (a coordinator-external
    # ``ProcessorContext``, e.g. a test) means "disabled", matching
    # ``image_ocr``'s own config default.
    image_ocr: bool | None = None
    # The generation this run's derived rows should be tagged with --
    # always files.generation + 1, matching the bump files_repo.mark_indexed
    # applies right after a processor returns successfully. Writing this
    # tag in the same delete+insert transaction as the entities/relationships
    # (see code/processor.py) is what makes that half of the work atomic;
    # the tiny files.generation bump immediately after is a separate,
    # near-instantaneous transaction that cannot itself leave entities
    # half-written since it touches no entity/relationship row.
    next_generation: int = 0


@dataclass(frozen=True)
class ProcessingOutcome:
    status: FileStatus


ProcessorFunc = Callable[[ProcessorContext], ProcessingOutcome]
# Search Quality Improvement Plan, Phase 12: a kind's *current* composite
# reuse identity, called fresh every time it's needed (never cached) so a
# live version-constant change (a real code change, or a test's
# monkeypatch) is always seen -- see ``VersionStamp``'s and
# ``decide_reprocessing``'s docstrings in ``indexing/incremental.py``.
VersionProviderFunc = Callable[[], VersionStamp]


def raw_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    return ProcessingOutcome(status=FileStatus.INDEXED)


class ProcessorRegistry:
    def __init__(self) -> None:
        self._processors: dict[FileKind, ProcessorFunc] = {}
        self._version_providers: dict[FileKind, VersionProviderFunc] = {}

    def register(
        self,
        kind: FileKind,
        processor: ProcessorFunc,
        *,
        version_provider: VersionProviderFunc | None = None,
    ) -> None:
        self._processors[kind] = processor
        if version_provider is not None:
            self._version_providers[kind] = version_provider

    def get(self, kind: FileKind) -> ProcessorFunc:
        return self._processors.get(kind, raw_processor)

    def get_version_provider(self, kind: FileKind) -> VersionProviderFunc | None:
        """``None`` for any kind whose registered processor has no
        version-tracked derivation of its own (the default raw processor,
        or a real processor -- e.g. today's ``code_processor`` -- that
        simply hasn't opted in yet). A file of such a kind is never
        subject to Phase 12's version-triggered reprocessing:
        ``IndexCoordinator`` falls back to pure content-hash reuse for it,
        exactly today's pre-Phase-12 behavior.
        """
        return self._version_providers.get(kind)


def default_registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    return registry


@dataclass
class IndexRunResult:
    scanned: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    # Search Quality Improvement Plan, Phase 12: a scanned file with no
    # exact path match that was matched back to an existing file record by
    # content hash instead (see ``_reconcile_renames``) -- its row was
    # updated in place (same id, new path), never deleted+recreated, so it
    # is counted separately from both ``new`` and ``deleted`` rather than
    # inflating either.
    moved: int = 0
    indexed: int = 0
    skipped_limit: int = 0
    failed: int = 0
    # File ids this run actually (re)indexed with derived content, split
    # by kind -- Phase 4's cross-domain linking pass (cli/index.py) is
    # scoped to these rather than the whole project, so an index run's
    # cost stays proportional to what changed. A file kept UNCHANGED
    # never reaches _process_queue at all, and one that was only
    # SKIPPED_LIMIT produced no entities/document content to link, so
    # neither is included here.
    touched_code_file_ids: list[str] = field(default_factory=list)
    touched_document_file_ids: list[str] = field(default_factory=list)
    # Search Quality Improvement Plan, Phase 12: files whose *content* is
    # unchanged (never queued, never reprocessed structurally -- unlike
    # touched_*_file_ids above) but whose stored embedding_model_id/
    # embedding_text_version stamp is stale, e.g. the configured embedding
    # model changed since this file was last embedded
    # (``decide_reprocessing`` returned ``EMBEDDINGS_ONLY``). Kept
    # separate from touched_*_file_ids rather than merged into them:
    # ``indexing/runner.py``'s cross-domain linking pass is scoped to
    # touched_*_file_ids only, since a file whose entities/document
    # sections never changed has nothing new to link -- relinking it would
    # be pure waste. The embedding step, the one place these are actually
    # read, unions both lists (see ``indexing/runner.py``).
    embeddings_stale_code_file_ids: list[str] = field(default_factory=list)
    embeddings_stale_document_file_ids: list[str] = field(default_factory=list)
    # Set when the source root itself could not be listed this run (see
    # ``sources.scanner.check_root_accessible``) -- every other field
    # above is left at its zero value, since no scan was attempted at
    # all. Distinct from a per-file failure: this is "the whole source
    # was unreachable", not "one file in it was bad".
    source_offline: bool = False
    offline_reason: str | None = None
    # Indexing optimization plan, Phase P1 / finding F6: set when
    # ``scan()`` could not fully list every subtree (a directory read
    # failure mid-walk, or a file whose stat() failed after being
    # listed) -- distinct from ``source_offline`` above, which means the
    # root itself was never reachable at all. Deletion reconciliation
    # (``find_deleted``) is skipped whenever this is true: a scan that
    # silently dropped part of the tree must never be trusted to tell a
    # genuinely deleted file apart from one merely unreadable this run.
    # New/changed files found in whatever *was* successfully scanned are
    # still processed -- only inferring "missing means deleted" is
    # unsafe, not the whole pass.
    scan_incomplete: bool = False
    scan_errors: list[str] = field(default_factory=list)


class IndexCoordinator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        source_path: str,
        include_patterns: list[str],
        exclude_patterns: list[str],
        config: RagMonkConfig,
        *,
        processors: ProcessorRegistry | None = None,
    ) -> None:
        self._conn = conn
        self._source_id = source_id
        self._root = Path(source_path)
        self._include = include_patterns
        self._exclude = exclude_patterns
        self._config = config
        self._processors = processors or default_registry()

    def _reconcile_renames(
        self,
        result: IndexRunResult,
        scanned: list[ScannedFile],
        existing_by_path: dict[str, FileRecord],
        algorithm: str,
    ) -> dict[str, str]:
        """Detects a same-source path change (move/rename) by content
        hash and reassigns the existing file row's path in place --
        keeping its id, and therefore every derived document_sections/
        entities/embeddings row intact -- rather than the delete-then-
        insert-as-new a plain path mismatch would otherwise cause
        (``find_deleted`` would drop the old path as deleted, the new
        path would classify ``NEW``, and every derived row would be
        rebuilt from scratch for no reason: the content never changed).

        Only a scanned path with *no* exact existing-path match is a
        rename candidate, and only when its content hash matches exactly
        one file that's about to look deleted (a path present in
        ``existing_by_path`` but absent from this scan) *and* that file
        was classified the same ``FileKind`` its new path would be --
        an ambiguous match (0 or 2+ candidates sharing that hash, or a
        kind change) is left alone rather than guessed at, falling back
        to today's delete+recreate. This mirrors, at the downstream
        chunk/embedding level, the reuse the content-hash-keyed
        ``document_conversion_cache`` (Phase 1B) already gives the raw
        PDF conversion step for a moved/renamed file.

        Mutates ``existing_by_path`` in place for every rename it
        performs, and returns every rename candidate's freshly computed
        content hash keyed by its scanned path -- callers reuse this
        instead of hashing the same file twice.
        """
        scanned_paths = {sf.path for sf in scanned}
        missing_by_hash: dict[str, list[FileRecord]] = {}
        for path, rec in existing_by_path.items():
            if path not in scanned_paths and rec.content_hash is not None:
                missing_by_hash.setdefault(rec.content_hash, []).append(rec)

        precomputed: dict[str, str] = {}
        if not missing_by_hash:
            return precomputed

        now = _now()
        for sf in scanned:
            if sf.path in existing_by_path:
                continue
            content_hash = hash_file(Path(sf.path), algorithm)
            precomputed[sf.path] = content_hash
            candidates = missing_by_hash.get(content_hash, [])
            if len(candidates) != 1:
                continue
            old = candidates[0]
            if old.kind is not detector.classify(Path(sf.path)):
                continue
            candidates.pop()
            files_repo.rename(
                self._conn, old.id, new_path=sf.path, size=sf.size, mtime=sf.mtime, updated_at=now
            )
            del existing_by_path[old.path]
            existing_by_path[sf.path] = old.model_copy(
                update={"path": sf.path, "size": sf.size, "mtime": sf.mtime, "updated_at": now}
            )
            result.moved += 1
        return precomputed

    def _version_reprocess_decision(
        self, prev: FileRecord, kind: FileKind
    ) -> ReprocessDecision:
        """``ReprocessDecision.NONE`` whenever ``kind`` has no registered
        version provider (see ``ProcessorRegistry.get_version_provider``)
        -- today, every kind except ``FileKind.DOCUMENT`` -- preserving
        pure content-hash reuse for those exactly as before Phase 12.
        """
        provider = self._processors.get_version_provider(kind)
        if provider is None:
            return ReprocessDecision.NONE
        current = provider()
        existing_versions = VersionStamp(
            parser_version=prev.parser_version,
            chunker_version=prev.chunker_version,
            embedding_model_id=prev.embedding_model_id,
            embedding_text_version=prev.embedding_text_version,
        )
        return decide_reprocessing(existing_versions, current)

    def run(self) -> IndexRunResult:
        result = IndexRunResult()

        # Checked before anything else -- including before
        # ``recover_stuck``, which is safe to defer, but scanning an
        # unreachable root and trusting its (empty) output would feed
        # ``find_deleted`` a false "every file is gone" diff. See
        # ``sources.scanner.check_root_accessible``'s docstring for why
        # ``os.walk`` alone can't be trusted to distinguish that from a
        # genuinely empty, reachable directory.
        offline_reason = check_root_accessible(self._root)
        if offline_reason is not None:
            result.source_offline = True
            result.offline_reason = offline_reason
            return result

        jobs_repo.recover_stuck(self._conn)

        guard = PathGuard([self._root])
        ignore_matcher = IgnoreMatcher(
            root=self._root, extra_patterns=self._exclude, include_patterns=self._include
        )
        scan_outcome = ScanOutcome()
        scanned = list(
            scan(
                self._root,
                guard=guard,
                ignore_matcher=ignore_matcher,
                follow_symlinks=self._config.indexing.follow_symlinks,
                outcome=scan_outcome,
            )
        )
        result.scanned = len(scanned)
        result.scan_incomplete = not scan_outcome.complete
        result.scan_errors = [f"{e.path}: {e.message}" for e in scan_outcome.errors]
        if result.scan_incomplete:
            log_event(
                _logger,
                "scan_incomplete",
                level=logging.WARNING,
                source_id=self._source_id,
                error_count=len(scan_outcome.errors),
                errors=result.scan_errors[:10],
            )

        existing = files_repo.list_by_source(self._conn, self._source_id)
        existing_by_path = {rec.path: rec for rec in existing}

        algorithm = self._config.indexing.hash_algorithm
        # Search Quality Improvement Plan, Phase 12: reassigns a moved/
        # renamed file's existing row in place (mutating existing_by_path
        # to match) *before* find_deleted runs, so its old path is never
        # seen as deleted and its new path is never seen as new -- see
        # ``_reconcile_renames``'s own docstring. Every path it resolves
        # this way also gets its content hash precomputed, reused by
        # classify_change below instead of hashing the same file twice.
        precomputed_hashes = self._reconcile_renames(result, scanned, existing_by_path, algorithm)

        # Indexing optimization plan, Phase P1 / finding F6: an
        # incomplete scan must never drive deletion reconciliation -- a
        # subtree ``scan()`` could not list would otherwise make every
        # file under it look deleted. New/changed files found in
        # whatever *was* successfully scanned are still processed below;
        # only "missing means deleted" is unsafe on a partial scan.
        if not result.scan_incomplete:
            for rec in find_deleted(existing_by_path, scanned):
                files_repo.delete(self._conn, rec.id)
                result.deleted += 1

        max_size_bytes = self._config.indexing.max_file_size_mb * 1024 * 1024

        for sf in scanned:
            prev = existing_by_path.get(sf.path)
            kind = detector.classify(Path(sf.path))

            cached_hash = precomputed_hashes.get(sf.path)

            def _lazy_hash(
                path: str = sf.path, algo: str = algorithm, cached: str | None = cached_hash
            ) -> str:
                return cached if cached is not None else hash_file(Path(path), algo)

            change, content_hash = classify_change(prev, sf.size, sf.mtime, _lazy_hash)

            if change is ChangeType.UNCHANGED and prev is not None:
                reprocess = self._version_reprocess_decision(prev, kind)
                if reprocess is ReprocessDecision.EMBEDDINGS_ONLY:
                    result.unchanged += 1
                    if kind is FileKind.CODE:
                        result.embeddings_stale_code_file_ids.append(prev.id)
                    else:
                        result.embeddings_stale_document_file_ids.append(prev.id)
                    continue
                if reprocess is ReprocessDecision.FULL:
                    # Content itself never changed, but the stored parser/
                    # chunker version stamp no longer matches current
                    # code -- forces exactly the same full reprocess a
                    # genuinely CHANGED file gets (see decide_reprocessing).
                    change = ChangeType.CHANGED

            if change is ChangeType.UNCHANGED:
                result.unchanged += 1
                continue

            now = _now()
            if prev is None:
                file_id = uuid.uuid4().hex
                new_record = FileRecord(
                    id=file_id,
                    source_id=self._source_id,
                    path=sf.path,
                    kind=kind,
                    size=sf.size,
                    mtime=sf.mtime,
                    content_hash=content_hash,
                    status=FileStatus.QUEUED,
                    generation=0,
                    created_at=now,
                    updated_at=now,
                )
                files_repo.insert(self._conn, new_record)
                # scan()'s own intra-run dedup should already rule this
                # out, but existing_by_path is otherwise a snapshot taken
                # once, before this loop -- keeping it in sync as we go
                # is what makes a same-run duplicate scanned path
                # (whatever produced it) UNCHANGED on its second sighting
                # instead of a second, UNIQUE-constraint-violating insert.
                existing_by_path[sf.path] = new_record
                result.new += 1
            else:
                file_id = prev.id
                files_repo.update_status(self._conn, file_id, FileStatus.QUEUED, updated_at=now)
                result.changed += 1

            jobs_repo.enqueue(self._conn, source_id=self._source_id, file_id=file_id)

        self._process_queue(result, max_size_bytes)
        return result

    def _process_queue(self, result: IndexRunResult, max_size_bytes: int) -> None:
        while True:
            job = jobs_repo.claim_next(self._conn)
            if job is None:
                break
            file = files_repo.get(self._conn, job.file_id)
            if file is None:
                jobs_repo.complete(self._conn, job.id)
                continue

            files_repo.update_status(self._conn, file.id, FileStatus.PROCESSING, updated_at=_now())
            processor = self._processors.get(file.kind)
            ctx = ProcessorContext(
                path=Path(file.path),
                size=file.size,
                kind=file.kind,
                max_size_bytes=max_size_bytes,
                conn=self._conn,
                source_id=self._source_id,
                file_id=file.id,
                source_root=self._root,
                next_generation=file.generation + 1,
                max_document_pages=self._config.documents.max_pages,
                chunking=self._config.documents.chunking,
                ocr=self._config.documents.ocr,
                image_ocr=self._config.documents.image_ocr,
            )
            started = time.monotonic()
            try:
                outcome = processor(ctx)
            except Exception as exc:  # noqa: BLE001 - a poisoned file must not abort the run
                attempt = job.attempt_count + 1
                permanent = retry.is_permanent(attempt)
                jobs_repo.fail_with_backoff(
                    self._conn,
                    job.id,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    next_attempt_at=None if permanent else retry.next_attempt_at(attempt),
                    permanent=permanent,
                )
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                if permanent:
                    files_repo.mark_failed(self._conn, file.id, error=str(exc), updated_at=_now())
                    errors_repo.record(
                        self._conn,
                        source_id=self._source_id,
                        file_id=file.id,
                        path=file.path,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                    result.failed += 1
                    log_event(
                        _logger,
                        "file_failed",
                        level=logging.WARNING,
                        source_id=self._source_id,
                        file_id=file.id,
                        duration_ms=duration_ms,
                        error_code=type(exc).__name__,
                    )
                else:
                    files_repo.update_status(
                        self._conn, file.id, FileStatus.RETRY, updated_at=_now()
                    )
                    log_event(
                        _logger,
                        "file_retry_scheduled",
                        level=logging.INFO,
                        source_id=self._source_id,
                        file_id=file.id,
                        attempt=attempt,
                    )
            else:
                # Search Quality Improvement Plan, Phase 12: stamp the
                # version-set that just produced this file's derived rows
                # -- but only when the processor actually produced any
                # (SKIPPED_LIMIT means it didn't touch document_sections/
                # entities at all, so stamping a version here would claim
                # a rebuild that never happened).
                provider = self._processors.get_version_provider(file.kind)
                versions = (
                    provider()
                    if provider is not None and outcome.status is not FileStatus.SKIPPED_LIMIT
                    else None
                )
                files_repo.mark_indexed(
                    self._conn,
                    file.id,
                    size=file.size,
                    mtime=file.mtime,
                    content_hash=file.content_hash,
                    status=outcome.status,
                    indexed_at=_now(),
                    parser_version=versions.parser_version if versions else None,
                    chunker_version=versions.chunker_version if versions else None,
                )
                jobs_repo.complete(self._conn, job.id)
                duration_ms = round((time.monotonic() - started) * 1000, 2)
                if outcome.status is FileStatus.SKIPPED_LIMIT:
                    result.skipped_limit += 1
                else:
                    result.indexed += 1
                    if file.kind is FileKind.CODE:
                        result.touched_code_file_ids.append(file.id)
                    elif file.kind is FileKind.DOCUMENT:
                        result.touched_document_file_ids.append(file.id)
                log_event(
                    _logger,
                    "file_indexed",
                    source_id=self._source_id,
                    file_id=file.id,
                    duration_ms=duration_ms,
                    status=outcome.status.value,
                )
