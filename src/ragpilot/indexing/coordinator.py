"""Orchestrates scan -> classify -> enqueue -> process for one source.

The processor registry is the extension point later phases hook into:
Phase 2/3 register real code/document processors keyed by ``FileKind``;
Phase 1 ships only the default "raw" processor, which just records file
metadata and marks the file INDEXED (or SKIPPED_LIMIT if oversized).
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

from ragpilot.core.config import RagpilotConfig
from ragpilot.core.models import FileKind, FileRecord, FileStatus
from ragpilot.indexing import retry
from ragpilot.indexing.incremental import ChangeType, classify_change, find_deleted
from ragpilot.security.path_guard import PathGuard
from ragpilot.sources import detector
from ragpilot.sources.fingerprint import hash_file
from ragpilot.sources.ignore import IgnoreMatcher
from ragpilot.sources.scanner import check_root_accessible, scan
from ragpilot.storage.repositories import errors_repo, files_repo, jobs_repo
from ragpilot.telemetry.logging import get_logger, log_event

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


def raw_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    return ProcessingOutcome(status=FileStatus.INDEXED)


class ProcessorRegistry:
    def __init__(self) -> None:
        self._processors: dict[FileKind, ProcessorFunc] = {}

    def register(self, kind: FileKind, processor: ProcessorFunc) -> None:
        self._processors[kind] = processor

    def get(self, kind: FileKind) -> ProcessorFunc:
        return self._processors.get(kind, raw_processor)


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
    # Set when the source root itself could not be listed this run (see
    # ``sources.scanner.check_root_accessible``) -- every other field
    # above is left at its zero value, since no scan was attempted at
    # all. Distinct from a per-file failure: this is "the whole source
    # was unreachable", not "one file in it was bad".
    source_offline: bool = False
    offline_reason: str | None = None


class IndexCoordinator:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        source_path: str,
        include_patterns: list[str],
        exclude_patterns: list[str],
        config: RagpilotConfig,
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
        scanned = list(
            scan(
                self._root,
                guard=guard,
                ignore_matcher=ignore_matcher,
                follow_symlinks=self._config.indexing.follow_symlinks,
            )
        )
        result.scanned = len(scanned)

        existing = files_repo.list_by_source(self._conn, self._source_id)
        existing_by_path = {rec.path: rec for rec in existing}

        for rec in find_deleted(existing_by_path, scanned):
            files_repo.delete(self._conn, rec.id)
            result.deleted += 1

        max_size_bytes = self._config.indexing.max_file_size_mb * 1024 * 1024
        algorithm = self._config.indexing.hash_algorithm

        for sf in scanned:
            prev = existing_by_path.get(sf.path)
            kind = detector.classify(Path(sf.path))

            def _lazy_hash(path: str = sf.path, algo: str = algorithm) -> str:
                return hash_file(Path(path), algo)

            change, content_hash = classify_change(prev, sf.size, sf.mtime, _lazy_hash)

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
                files_repo.mark_indexed(
                    self._conn,
                    file.id,
                    size=file.size,
                    mtime=file.mtime,
                    content_hash=file.content_hash,
                    status=outcome.status,
                    indexed_at=_now(),
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
