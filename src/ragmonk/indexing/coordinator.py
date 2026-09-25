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
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from ragmonk.core.config import ChunkingConfig, RagMonkConfig
from ragmonk.core.errors import SecurityViolationError
from ragmonk.core.models import FileKind, FileRecord, FileStatus, IndexJob, ScannedFile
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
from ragmonk.sources.fingerprint import FileIdentity, hash_file
from ragmonk.sources.ignore import IgnoreMatcher
from ragmonk.sources.scanner import ScanOutcome, check_root_accessible, scan
from ragmonk.storage.repositories import errors_repo, files_repo, jobs_repo
from ragmonk.storage.sqlite import connect as sqlite_connect
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
    # Indexing optimization plan, Phase P3 / finding F4: the content
    # hash this coordinator run already computed for this file (during
    # scan/classify_change), plus the exact size/mtime it was verified
    # against. A processor uses ``fingerprint.verified_hash`` with this
    # instead of unconditionally re-reading and re-hashing the whole
    # file -- see ``documents/pipeline.py``. ``None`` for a
    # coordinator-external ``ProcessorContext`` (most unit tests, a
    # direct processor call) -- every such caller keeps hashing the
    # file itself, exactly as before this phase.
    file_identity: FileIdentity | None = None


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
# Indexing optimization plan, Phase P4 (CODE) / V2 Phase P2 (DOCUMENT): the
# optional split a kind can register instead of (or alongside) a plain
# ``ProcessorFunc`` -- a "prepare" half, safe to run concurrently across
# files in a bounded worker pool, and a "publish" half that takes its
# result and does the transactional write, always on the coordinator's
# single writer thread. See ``code.processor``'s ``prepare_code``
# (``(path, source_root) -> PreparedCode``) and ``documents.pipeline``'s
# ``prepare_document`` (``(ctx, *, cache_conn) -> PreparedDocument``) for
# the two real implementations -- deliberately typed ``Callable[...,
# Any]`` here rather than one fixed signature, since each kind's own
# prepare function needs different inputs (CODE needs no database access
# at all; DOCUMENT needs the document-conversion cache, via a dedicated
# per-worker connection -- see ``_PerThreadConnections`` -- never the
# coordinator's own shared ``self._conn``, which is not safe to use
# concurrently from multiple threads). ``_process_queue_with_parallel``
# below is what actually knows how to call each kind's prepare function;
# ``ProcessorRegistry`` itself never inspects either the callable's
# signature or its opaque prepared-value's type, both of which flow
# straight from one kind's prepare to that same kind's publish.
PrepareFunc = Callable[..., Any]
PublishFunc = Callable[[ProcessorContext, Any], ProcessingOutcome]
# V2 Phase P2: an optional one-time setup hook, called with the
# configured worker count the first time a kind's parallel prepare path
# is actually engaged for a run -- e.g. ``documents.docling_adapter.
# cap_native_thread_pools``, which caps torch's intra-op thread pool so
# ``document_extraction_workers`` concurrent Docling conversions don't
# each try to claim every CPU core. ``None`` (CODE's registration, and
# every kind that has no native worker pool of its own to worry about)
# means no setup is needed.
PrepareSetupFunc = Callable[[int], None]


def raw_processor(ctx: ProcessorContext) -> ProcessingOutcome:
    if ctx.size > ctx.max_size_bytes:
        return ProcessingOutcome(status=FileStatus.SKIPPED_LIMIT)
    return ProcessingOutcome(status=FileStatus.INDEXED)


class ProcessorRegistry:
    def __init__(self) -> None:
        self._processors: dict[FileKind, ProcessorFunc] = {}
        self._version_providers: dict[FileKind, VersionProviderFunc] = {}
        self._prepare_funcs: dict[FileKind, PrepareFunc] = {}
        self._publish_funcs: dict[FileKind, PublishFunc] = {}
        self._prepare_setup_funcs: dict[FileKind, PrepareSetupFunc] = {}

    def register(
        self,
        kind: FileKind,
        processor: ProcessorFunc,
        *,
        version_provider: VersionProviderFunc | None = None,
        prepare: PrepareFunc | None = None,
        publish: PublishFunc | None = None,
        prepare_setup: PrepareSetupFunc | None = None,
    ) -> None:
        self._processors[kind] = processor
        if version_provider is not None:
            self._version_providers[kind] = version_provider
        # Indexing optimization plan, Phase P4: both halves or neither --
        # a kind with only one of the two would leave the coordinator's
        # parallel path with no way to publish (or nothing to prepare
        # ahead of time), so a kind not offering the full split is
        # simply never eligible for it (see ``ProcessorRegistry.
        # supports_parallel_prepare``), falling back to ``processor``
        # exactly like today.
        if prepare is not None and publish is not None:
            self._prepare_funcs[kind] = prepare
            self._publish_funcs[kind] = publish
            if prepare_setup is not None:
                self._prepare_setup_funcs[kind] = prepare_setup

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

    def supports_parallel_prepare(self, kind: FileKind) -> bool:
        return kind in self._prepare_funcs

    def get_prepare(self, kind: FileKind) -> PrepareFunc:
        return self._prepare_funcs[kind]

    def get_publish(self, kind: FileKind) -> PublishFunc:
        return self._publish_funcs[kind]

    def get_prepare_setup(self, kind: FileKind) -> PrepareSetupFunc | None:
        return self._prepare_setup_funcs.get(kind)


def default_registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    for kind in FileKind:
        registry.register(kind, raw_processor)
    return registry


@dataclass
class StageTimings:
    """Indexing optimization plan V2, Phase P5: wall-clock duration of
    each stage a source pass naturally already goes through, so a slow
    run's dominant stage is visible without attaching a profiler.
    Populated at the boundaries the coordinator/runner code already has
    (this phase added no new intermediate steps to time, only timers
    around existing ones):

    - ``scan``: the tree walk (full mode, ``sources.scanner.scan``) or
      the per-path re-stat loop (targeted mode, Phase P2) that turns a
      trigger into a list of candidate files.
    - ``classify``: ``indexing.incremental.classify_change`` plus every
      version-reprocess-decision check, rename reconciliation, and the
      new/changed/unchanged bookkeeping/job-enqueue loop -- includes
      ``hash`` below as a sub-cost, not a separate pass over the files.
    - ``hash``: the ``hash_calls``/``hash_seconds`` subset of
      ``classify`` actually spent inside ``sources.fingerprint.hash_file``
      -- broken out because it is classify's one genuinely IO-bound part
      (Phase P3's ``verified_hash`` reuse already avoids a second,
      independent hash of the same file downstream in the document/code
      processors; this is the coordinator's own, first hash).
    - ``process``: ``_process_queue``'s prepare+publish work for every
      queued file this pass -- not split further into "prepare" and
      "publish" sub-timings: Phase P2/P4's bounded-parallel path already
      interleaves many files' prepare calls with the single writer
      thread's publish calls (see ``_process_queue_with_parallel``), so
      there is no single clean boundary between the two to time
      separately without either misleading numbers (attributing a
      worker's idle wait time to "publish") or real, non-trivial
      per-file instrumentation this phase's own "use boundaries that
      already exist" rule argues against inventing. A future phase with
      an actual need for that finer split should add it deliberately,
      not as a byproduct of this one.
    - ``linking``: ``knowledge.linker.link_touched_files``
      (``indexing/runner.py``'s ``run_source_pass``, unchanged boundary
      from Phase 4/P6).
    - ``embedding``: ``indexing.embedding_indexer.prepare_embeddings`` +
      ``publish_embeddings`` combined (``run_source_pass`` again) --
      the P5(v1)/P3(v2) split between model inference and the write
      transaction is a *safety* boundary (write-lock duration), not a
      cost one; both still count as "the embedding stage" here.
    - ``ann_sync``: ``retrieval.ann.sync_index_for_files``
      (``run_source_pass``, gated the same way ``embedding`` is, behind
      ``search.semantic``).

    All zero for a pass that never reaches that stage (e.g.
    ``ann_sync_seconds`` stays ``0.0`` whenever ``search.semantic`` is
    off, or nothing was embedded this pass).
    """

    scan_seconds: float = 0.0
    classify_seconds: float = 0.0
    hash_seconds: float = 0.0
    hash_calls: int = 0
    process_seconds: float = 0.0
    linking_seconds: float = 0.0
    embedding_seconds: float = 0.0
    ann_sync_seconds: float = 0.0


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
    # Indexing optimization plan, Phase P2: True when this run only
    # examined ``ScanRequest.changed_paths`` rather than walking the
    # whole source tree -- telemetry only (see ``daemon_pass_completed``
    # in ``service/daemon.py``), never something a caller branches on.
    targeted: bool = False
    # Indexing optimization plan V2, Phase P5: per-stage wall-clock
    # durations for this pass -- see ``StageTimings``'s own docstring.
    timings: StageTimings = field(default_factory=StageTimings)


# Indexing optimization plan, Phase P2: the unit of work a caller hands
# ``IndexCoordinator`` -- either "walk and diff the whole tree"
# (``full=True``, the only mode that existed before this phase and the
# only one ``run_source_pass``'s own callers get unless they explicitly
# opt in) or "these specific paths changed, work from that instead"
# (``full=False`` with ``changed_paths`` set). ``reason`` is telemetry
# only (which trigger produced this request), matching ``Daemon.
# enqueue_source``'s own ``reason`` parameter.
@dataclass(frozen=True)
class ScanRequest:
    source_id: str
    reason: str = "manual"
    changed_paths: frozenset[str] | None = None
    full: bool = True

    def __post_init__(self) -> None:
        if not self.full and not self.changed_paths:
            raise ValueError("a non-full ScanRequest needs at least one changed path")


class _PerThreadConnections:
    """Indexing optimization plan V2, Phase P2: lazily opens one extra
    ``sqlite3.Connection`` to the *same* database file per worker thread
    that calls ``get()`` -- used only by a parallel-eligible kind's
    prepare function for its own read/write needs during the prepare
    phase (today, only ``documents.pipeline.prepare_document``'s
    document-conversion cache lookup/store), never for the coordinator's
    own ``self._conn``, which stays the single writer connection used
    exclusively by ``publish`` calls on this method's own thread.

    A ``sqlite3.Connection`` object is not safe to use concurrently from
    more than one thread, so handing every parallel worker the *same*
    connection object (even just for reads) risks exactly the
    "sqlite shared-connection/thread errors" this phase's acceptance
    criteria rule out. Opening a genuinely separate connection per thread
    instead is safe under this project's standard WAL + busy-timeout
    setup (``storage/sqlite.connect``) -- the same precedent
    ``storage/sqlite.py``'s own docstring already establishes for the
    Phase 7 daemon's worker/reconciliation threads, just one connection
    per thread here instead of one shared connection guarded by a lock.

    ``close_all()`` is called once, after the owning ``ThreadPoolExecutor``
    has fully drained (see ``_process_queue_with_parallel``'s ``ExitStack``
    usage) -- by then no worker thread can still be using its connection.
    """

    def __init__(self, db_path: Path, *, cache_size_mb: int = 64) -> None:
        self._db_path = db_path
        self._cache_size_mb = cache_size_mb
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite_connect(self._db_path, cache_size_mb=self._cache_size_mb)
            self._local.conn = conn
            with self._lock:
                self._opened.append(conn)
        return conn

    def close_all(self) -> None:
        with self._lock:
            opened, self._opened = self._opened, []
        for conn in opened:
            conn.close()


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
        # Indexing optimization plan, Phase P3: this run's file_id ->
        # verified FileIdentity, populated during the scan loop below
        # and consumed (popped) in ``_process_queue`` when building each
        # file's ``ProcessorContext``. Reset at the top of every ``run()``
        # call, not just here, since a caller may reuse one coordinator
        # instance across multiple runs (tests do).
        self._pending_identities: dict[str, FileIdentity] = {}

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
            content_hash = self._timed_hash(result, sf.path, algorithm)
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

    def _timed_hash(self, result: IndexRunResult, path: str, algorithm: str) -> str:
        """``sources.fingerprint.hash_file``, with its cost folded into
        ``result.timings.hash_seconds``/``hash_calls`` (Phase P5) --
        every direct ``hash_file`` call in this class goes through this
        one method instead, so the two counters always describe every
        hash this coordinator itself performed, regardless of which of
        the several call sites (full scan, targeted new/changed/renamed
        paths) needed it.
        """
        started = time.monotonic()
        digest = hash_file(Path(path), algorithm)
        result.timings.hash_seconds += time.monotonic() - started
        result.timings.hash_calls += 1
        return digest

    def _version_reprocess_decision(self, prev: FileRecord, kind: FileKind) -> ReprocessDecision:
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

    def run(self, *, changed_paths: frozenset[str] | None = None) -> IndexRunResult:
        """``changed_paths`` (Phase P2), when given and non-empty, skips
        the full tree walk entirely and instead examines exactly these
        paths -- see ``_run_targeted``. ``None`` (every pre-P2 caller)
        keeps this method's original full-scan behavior unchanged.
        """
        if changed_paths:
            return self._run_targeted(changed_paths)

        result = IndexRunResult()
        self._pending_identities = {}

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
        _scan_started = time.monotonic()
        scanned = list(
            scan(
                self._root,
                guard=guard,
                ignore_matcher=ignore_matcher,
                follow_symlinks=self._config.indexing.follow_symlinks,
                outcome=scan_outcome,
            )
        )
        result.timings.scan_seconds = time.monotonic() - _scan_started
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

        _classify_started = time.monotonic()
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
                return cached if cached is not None else self._timed_hash(result, path, algo)

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
                files_repo.update_status(
                    self._conn,
                    file_id,
                    FileStatus.QUEUED,
                    updated_at=now,
                    size=sf.size,
                    mtime=sf.mtime,
                    content_hash=content_hash,
                )
                result.changed += 1

            # Indexing optimization plan, Phase P3: this file_id's
            # verified identity for whatever processor runs it next --
            # see ``_pending_identities``'s docstring in ``__init__``.
            self._pending_identities[file_id] = FileIdentity(
                content_hash=content_hash, size=sf.size, mtime=sf.mtime
            )
            jobs_repo.enqueue(self._conn, source_id=self._source_id, file_id=file_id)

        result.timings.classify_seconds = time.monotonic() - _classify_started
        _process_started = time.monotonic()
        self._process_queue(result, max_size_bytes)
        result.timings.process_seconds = time.monotonic() - _process_started
        return result

    def _run_targeted(self, changed_paths: frozenset[str]) -> IndexRunResult:
        """Indexing optimization plan, Phase P2: examines exactly
        ``changed_paths`` instead of walking the whole source tree.

        Safe by construction against finding F6 -- there is no directory
        walk to silently truncate; every path's fate is decided by its
        own individually-checked ``stat()``. Deletion is equally
        precise: only a path explicitly named here, with an existing
        record, is ever deleted, never inferred from absence in a walk.

        Rename identity (``files_repo.rename``, keeping the same file
        id and therefore every derived row) is preserved only when both
        halves of a rename -- the old path going missing, the new path
        appearing, with byte-identical content -- land in this same
        batch, which the watcher's own debouncing makes the common case
        for a local editor/``git mv``. A rename split across two
        separate targeted batches degrades to delete-then-recreate for
        this pass, exactly as an unrelated new file at that path would;
        periodic full reconciliation does not retroactively undo that
        (its own rename detection needs the *old* row still present).
        This is an accepted, disclosed trade-off for this phase -- see
        the plan's own "force full scan on ... ambiguous rename"
        guidance, read here as "only resolve the unambiguous, same-
        batch case; anything else keeps working, just without identity
        preservation".
        """
        result = IndexRunResult(targeted=True)
        self._pending_identities = {}

        # Same check the full-scan path makes before trusting anything
        # else -- a network source flickering offline between the
        # watcher's diff and this call must never be treated as "every
        # missing path was deleted" (exactly finding F6's concern, just
        # via a different trigger than an unreadable subtree). Bailing
        # out here also means a reconnect's large "everything looks new
        # again" diff is never processed while still offline; the very
        # next successful pass (targeted or, once the daemon's own size
        # guard kicks in, full) reconciles it correctly regardless.
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
        follow_symlinks = self._config.indexing.follow_symlinks
        algorithm = self._config.indexing.hash_algorithm
        max_size_bytes = self._config.indexing.max_file_size_mb * 1024 * 1024
        now = _now()

        _scan_started = time.monotonic()
        present: dict[str, ScannedFile] = {}
        missing: set[str] = set()
        for raw_path in changed_paths:
            candidate = Path(raw_path)
            if not follow_symlinks and candidate.is_symlink():
                continue
            try:
                resolved = guard.resolve(candidate)
            except SecurityViolationError:
                continue
            resolved_str = str(resolved)
            try:
                stat = resolved.stat()
            except OSError:
                missing.add(resolved_str)
                continue
            if resolved.is_dir():
                # Directory events carry no information a contained
                # file event doesn't already provide -- matches
                # scan()/LocalSourceWatcher's own "directories are
                # ignored" rule.
                continue
            if ignore_matcher.is_ignored(resolved, is_dir=False):
                continue
            present[resolved_str] = ScannedFile(
                path=resolved_str, size=stat.st_size, mtime=stat.st_mtime
            )

        result.timings.scan_seconds = time.monotonic() - _scan_started
        result.scanned = len(present) + len(missing)

        _classify_started = time.monotonic()
        missing_records: dict[str, FileRecord] = {}
        for path in missing:
            record = files_repo.get_by_path(self._conn, self._source_id, path)
            if record is not None:
                missing_records[path] = record

        new_paths: dict[str, ScannedFile] = {}
        existing_records: dict[str, FileRecord] = {}
        for path, sf in present.items():
            record = files_repo.get_by_path(self._conn, self._source_id, path)
            if record is None:
                new_paths[path] = sf
            else:
                existing_records[path] = record

        # Same-batch rename reconciliation (see docstring): a `missing`
        # path with an existing record, whose content hash matches
        # exactly one `new` path's, is a move -- reuse that row's id in
        # place instead of delete+recreate. Ambiguous matches (0 or 2+
        # candidates, or a kind change) are left alone, falling back to
        # today's delete+recreate, exactly like ``_reconcile_renames``.
        renamed_from: set[str] = set()
        if missing_records and new_paths:
            missing_by_hash: dict[str, list[FileRecord]] = {}
            for record in missing_records.values():
                if record.content_hash is not None:
                    missing_by_hash.setdefault(record.content_hash, []).append(record)
            for path in list(new_paths):
                sf = new_paths[path]
                content_hash = self._timed_hash(result, path, algorithm)
                kind = detector.classify(Path(path))
                candidates = missing_by_hash.get(content_hash, [])
                if len(candidates) != 1 or candidates[0].kind is not kind:
                    continue
                old = candidates[0]
                candidates.pop()
                files_repo.rename(
                    self._conn, old.id, new_path=path, size=sf.size, mtime=sf.mtime, updated_at=now
                )
                renamed_from.add(old.path)
                result.moved += 1
                del new_paths[path]
                # Content is confirmed byte-identical to the old row
                # (that's the match condition above) -- nothing left to
                # reprocess for content, only a possible version-
                # triggered reprocess (Phase 12), exactly like a
                # full-scan rename gets via the main scan loop.
                reprocess = self._version_reprocess_decision(old, kind)
                if reprocess is ReprocessDecision.EMBEDDINGS_ONLY:
                    result.unchanged += 1
                    if kind is FileKind.CODE:
                        result.embeddings_stale_code_file_ids.append(old.id)
                    else:
                        result.embeddings_stale_document_file_ids.append(old.id)
                elif reprocess is ReprocessDecision.FULL:
                    files_repo.update_status(
                        self._conn,
                        old.id,
                        FileStatus.QUEUED,
                        updated_at=now,
                        size=sf.size,
                        mtime=sf.mtime,
                        content_hash=content_hash,
                    )
                    result.changed += 1
                    self._pending_identities[old.id] = FileIdentity(
                        content_hash=content_hash, size=sf.size, mtime=sf.mtime
                    )
                    jobs_repo.enqueue(self._conn, source_id=self._source_id, file_id=old.id)
                else:
                    result.unchanged += 1

        for path, record in missing_records.items():
            if path in renamed_from:
                continue
            files_repo.delete(self._conn, record.id)
            result.deleted += 1

        for path, sf in new_paths.items():
            kind = detector.classify(Path(path))
            content_hash = self._timed_hash(result, path, algorithm)
            file_id = uuid.uuid4().hex
            new_record = FileRecord(
                id=file_id,
                source_id=self._source_id,
                path=path,
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
            result.new += 1
            self._pending_identities[file_id] = FileIdentity(
                content_hash=content_hash, size=sf.size, mtime=sf.mtime
            )
            jobs_repo.enqueue(self._conn, source_id=self._source_id, file_id=file_id)

        for path, record in existing_records.items():
            sf = present[path]
            kind = detector.classify(Path(path))

            def _lazy_hash(p: str = path, algo: str = algorithm) -> str:
                return self._timed_hash(result, p, algo)

            change, content_hash = classify_change(record, sf.size, sf.mtime, _lazy_hash)

            if change is ChangeType.UNCHANGED:
                reprocess = self._version_reprocess_decision(record, kind)
                if reprocess is ReprocessDecision.EMBEDDINGS_ONLY:
                    result.unchanged += 1
                    if kind is FileKind.CODE:
                        result.embeddings_stale_code_file_ids.append(record.id)
                    else:
                        result.embeddings_stale_document_file_ids.append(record.id)
                    continue
                if reprocess is ReprocessDecision.FULL:
                    change = ChangeType.CHANGED

            if change is ChangeType.UNCHANGED:
                result.unchanged += 1
                continue

            files_repo.update_status(
                self._conn,
                record.id,
                FileStatus.QUEUED,
                updated_at=now,
                size=sf.size,
                mtime=sf.mtime,
                content_hash=content_hash,
            )
            result.changed += 1
            self._pending_identities[record.id] = FileIdentity(
                content_hash=content_hash, size=sf.size, mtime=sf.mtime
            )
            jobs_repo.enqueue(self._conn, source_id=self._source_id, file_id=record.id)

        result.timings.classify_seconds = time.monotonic() - _classify_started
        _process_started = time.monotonic()
        self._process_queue(result, max_size_bytes)
        result.timings.process_seconds = time.monotonic() - _process_started
        return result

    def _start_job(self, file: FileRecord, max_size_bytes: int) -> ProcessorContext:
        """Marks ``file`` PROCESSING and builds its ``ProcessorContext``
        -- shared by the serial and bounded-parallel paths below, called
        at the same point in each (right after a job is claimed) so
        both have identical PROCESSING-stamp and identity-consumption
        timing.
        """
        files_repo.update_status(self._conn, file.id, FileStatus.PROCESSING, updated_at=_now())
        # Indexing optimization plan, Phase P3: popped (not just read)
        # so a same-pass immediate retry of this file_id (identity
        # already consumed) safely falls back to re-hashing the file
        # itself, rather than reusing a possibly-stale identity twice.
        identity = self._pending_identities.pop(file.id, None)
        return ProcessorContext(
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
            file_identity=identity,
        )

    def _finish_job(
        self,
        result: IndexRunResult,
        job: IndexJob,
        file: FileRecord,
        started: float,
        run: Callable[[], ProcessingOutcome],
    ) -> None:
        """Runs ``run()`` (either ``processor(ctx)`` directly, or --
        Phase P4's bounded-parallel path -- ``publish(ctx,
        future.result())``, which blocks for the matching prepare
        future and re-raises its exception here on the writer thread if
        it failed) and handles the outcome exactly as the pre-P4
        ``_process_queue`` always did: identical failure/retry/success
        bookkeeping regardless of which path produced ``run``.
        """
        try:
            outcome = run()
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
                files_repo.update_status(self._conn, file.id, FileStatus.RETRY, updated_at=_now())
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

    def _document_cache_db_path(self) -> Path:
        """The on-disk path backing ``self._conn`` -- read straight off
        SQLite itself (``PRAGMA database_list``) rather than threading a
        separate path through every ``IndexCoordinator`` constructor
        call, since every existing caller (``run_source_pass``, every
        test) already only ever has the open connection, not its path.
        """
        row = self._conn.execute("PRAGMA database_list").fetchone()
        return Path(row["file"])

    def _process_queue(self, result: IndexRunResult, max_size_bytes: int) -> None:
        # Indexing optimization plan, Phase P4 (CODE) / V2 Phase P2
        # (DOCUMENT): bounded parallel extraction is opt-in per kind
        # (config default 1 for both = fully serial, byte-for-byte the
        # pre-P4 path below) and only ever applies to a kind that
        # registered the full prepare/publish split. Every other kind
        # (today: raw/unknown) always goes through the plain synchronous
        # path.
        code_workers = max(1, self._config.indexing.code_extraction_workers)
        document_workers = max(1, self._config.indexing.document_extraction_workers)
        parallel_kinds: dict[FileKind, tuple[int, Callable[[ProcessorContext], Any]]] = {}
        cache_pool: _PerThreadConnections | None = None

        if code_workers > 1 and self._processors.supports_parallel_prepare(FileKind.CODE):
            prepare_code = self._processors.get_prepare(FileKind.CODE)
            root = self._root
            parallel_kinds[FileKind.CODE] = (
                code_workers,
                lambda ctx, _prepare=prepare_code, _root=root: _prepare(ctx.path, _root),
            )

        if document_workers > 1 and self._processors.supports_parallel_prepare(FileKind.DOCUMENT):
            prepare_setup = self._processors.get_prepare_setup(FileKind.DOCUMENT)
            if prepare_setup is not None:
                # V2 Phase P2: cap native (torch) thread pools *before*
                # any concurrent Docling conversion actually starts --
                # see ``documents.docling_adapter.cap_native_thread_pools``.
                prepare_setup(document_workers)
            prepare_document = self._processors.get_prepare(FileKind.DOCUMENT)
            cache_pool = _PerThreadConnections(
                self._document_cache_db_path(),
                cache_size_mb=self._config.runtime.sqlite_cache_size_mb,
            )
            parallel_kinds[FileKind.DOCUMENT] = (
                document_workers,
                lambda ctx, _prepare=prepare_document, _pool=cache_pool: _prepare(
                    ctx, cache_conn=_pool.get()
                ),
            )

        if parallel_kinds:
            self._process_queue_with_parallel(result, max_size_bytes, parallel_kinds, cache_pool)
            return

        while True:
            job = jobs_repo.claim_next(self._conn)
            if job is None:
                break
            file = files_repo.get(self._conn, job.file_id)
            if file is None:
                jobs_repo.complete(self._conn, job.id)
                continue
            ctx = self._start_job(file, max_size_bytes)
            processor = self._processors.get(file.kind)
            started = time.monotonic()
            self._finish_job(result, job, file, started, partial(processor, ctx))

    def _process_queue_with_parallel(
        self,
        result: IndexRunResult,
        max_size_bytes: int,
        parallel_kinds: dict[FileKind, tuple[int, Callable[[ProcessorContext], Any]]],
        cache_pool: _PerThreadConnections | None,
    ) -> None:
        """Phase P4 (CODE) / V2 Phase P2 (DOCUMENT), generalized: each
        kind in ``parallel_kinds`` gets its own bounded thread pool and
        its own pending queue -- a job of that kind has its pure/read-
        only "prepare" half (``parallel_kinds[kind][1]``, already bound
        to that kind's real prepare function and whatever per-kind
        extras it needs -- see ``_process_queue``'s two closures)
        submitted there, up to that kind's own configured worker count
        in flight at once. The transactional "publish" half always runs
        synchronously here, on this method's own thread -- the
        coordinator's single writer, exactly the "one transactional
        publisher per project" safety rule, unchanged from Phase P4 and
        never violated by adding a second parallel-eligible kind.

        A job whose kind is *not* in ``parallel_kinds`` (today: raw/
        unknown, or a parallel-eligible kind whose own worker count is
        still 1) flushes every kind's pending queue first, then
        processes synchronously through the exact same path
        ``_process_queue`` always used -- deliberately conservative
        (flushing *all* kinds' queues, not just the one that would
        conflict) to keep cross-kind ordering simple and auditable
        rather than interleaving three pipelines' worth of bookkeeping.
        """
        pending: dict[
            FileKind, list[tuple[IndexJob, FileRecord, ProcessorContext, float, Future]]
        ] = {kind: [] for kind in parallel_kinds}

        def flush_one(kind: FileKind) -> None:
            job, file, ctx, started, future = pending[kind].pop(0)
            publish = self._processors.get_publish(file.kind)

            def run() -> ProcessingOutcome:
                # ``future.result()`` blocks for this file's prepare to
                # finish (if it hasn't already) and re-raises whatever
                # exception it raised -- caught by ``_finish_job`` here,
                # on the writer thread, exactly like a synchronous
                # processor's own exception.
                prepared = future.result()
                return publish(ctx, prepared)

            self._finish_job(result, job, file, started, run)

        def flush_all() -> None:
            for kind in parallel_kinds:
                while pending[kind]:
                    flush_one(kind)

        with ExitStack() as stack:
            executors = {
                kind: stack.enter_context(ThreadPoolExecutor(max_workers=workers))
                for kind, (workers, _prepare_call) in parallel_kinds.items()
            }
            if cache_pool is not None:
                # Closed only after every executor above has fully
                # drained (ExitStack unwinds in reverse registration
                # order, and this callback was registered after the
                # executors) -- no worker thread can still be mid-call
                # against one of these connections by the time this runs.
                stack.callback(cache_pool.close_all)

            while True:
                job = jobs_repo.claim_next(self._conn)
                if job is None:
                    flush_all()
                    break
                file = files_repo.get(self._conn, job.file_id)
                if file is None:
                    jobs_repo.complete(self._conn, job.id)
                    continue

                if file.kind in parallel_kinds:
                    workers, prepare_call = parallel_kinds[file.kind]
                    ctx = self._start_job(file, max_size_bytes)
                    started = time.monotonic()
                    future = executors[file.kind].submit(prepare_call, ctx)
                    pending[file.kind].append((job, file, ctx, started, future))
                    if len(pending[file.kind]) >= workers:
                        flush_one(file.kind)
                else:
                    flush_all()
                    ctx = self._start_job(file, max_size_bytes)
                    processor = self._processors.get(file.kind)
                    started = time.monotonic()
                    self._finish_job(result, job, file, started, partial(processor, ctx))
