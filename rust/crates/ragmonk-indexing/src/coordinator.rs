//! The V2 indexing coordinator.
//!
//! Per source: offline check → scan → diff against the reusable baseline
//! (empty for a full rebuild) → begin a build → carry unchanged files
//! forward → process new/changed/moved files → publish atomically, or abort
//! and leave the previously published build visible.
//!
//! Concurrency is bounded everywhere: `workers` prepare threads pull from a
//! `sync_channel` of capacity `2 * workers`, results flow back through a
//! channel of the same bound, and exactly one writer (the calling thread)
//! applies them to SQLite in one short transaction per file. Processors
//! never run inside a transaction. A panicking processor fails only its
//! file. A failing source never stops later sources ([`index_all`]).

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{sync_channel, Receiver};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use ragmonk_config::RagMonkConfig;
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::ids::v2;
use ragmonk_core::models::FileKind;
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_storage::control::{plan_for, ControlPlane, IndexVersions, RebuildPlan, SourceRecord};
use ragmonk_storage::knowledge::{FileKnowledge, FileRow, ProjectStore};
use ragmonk_storage::schema::V2_SCHEMA_VERSION;
use ragmonk_storage::V2Layout;
use serde::Serialize;

use crate::diff::{diff, Change, Decision, DiffCounts};
use crate::fingerprint::hash_file;
use crate::ignore::IgnoreMatcher;
use crate::lock::RunLock;
use crate::retry;
use crate::scan::{check_root_accessible, scan, ScanOptions};

/// What a processor sees for one file.
#[derive(Debug, Clone)]
pub struct PrepareInput {
    pub source_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub path: PathBuf,
    pub kind: FileKind,
    pub content_hash: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcessError {
    pub code: String,
    pub message: String,
    /// Transient errors are retried with backoff; others fail permanently.
    pub transient: bool,
}

/// Extracts knowledge from one file. Must be pure with respect to storage:
/// it runs on worker threads, outside any transaction.
pub trait Processor: Send + Sync {
    fn prepare(&self, input: &PrepareInput) -> Result<FileKnowledge, ProcessError>;
}

/// Records the file only (the reference's `raw_processor`). Code and
/// document extraction replace it in RUST-05/RUST-07.
pub struct RawProcessor;

impl Processor for RawProcessor {
    fn prepare(&self, input: &PrepareInput) -> Result<FileKnowledge, ProcessError> {
        std::fs::metadata(&input.path).map_err(|e| ProcessError {
            code: "io_error".into(),
            message: e.to_string(),
            transient: true,
        })?;
        Ok(FileKnowledge::default())
    }
}

/// Processors per file kind plus the versions they produce.
pub struct Registry {
    pub code: Arc<dyn Processor>,
    pub document: Arc<dyn Processor>,
    pub unknown: Arc<dyn Processor>,
    pub versions: IndexVersions,
}

impl Registry {
    pub fn raw() -> Self {
        let raw: Arc<dyn Processor> = Arc::new(RawProcessor);
        Self {
            code: raw.clone(),
            document: raw.clone(),
            unknown: raw,
            versions: IndexVersions {
                schema_version: V2_SCHEMA_VERSION,
                parser_version: "raw-1".into(),
                chunker_version: "none".into(),
                converter_version: "none".into(),
                embedding_model_id: None,
                embedding_text_version: None,
            },
        }
    }

    fn for_kind(&self, kind: FileKind) -> &dyn Processor {
        match kind {
            FileKind::Code => self.code.as_ref(),
            FileKind::Document => self.document.as_ref(),
            FileKind::Unknown => self.unknown.as_ref(),
        }
    }
}

#[derive(Debug, Clone, Serialize, PartialEq)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum ProgressEvent {
    SourceStarted {
        source_id: String,
        position: usize,
        total: usize,
    },
    Stage {
        source_id: String,
        stage: &'static str,
    },
    Scanned {
        source_id: String,
        files: usize,
        to_process: usize,
    },
    FileDone {
        source_id: String,
        done: usize,
        total: usize,
    },
    SourceFinished {
        source_id: String,
        ok: bool,
    },
}

pub trait Progress {
    fn event(&mut self, event: &ProgressEvent);
}

pub struct NoProgress;
impl Progress for NoProgress {
    fn event(&mut self, _: &ProgressEvent) {}
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct StageTimings {
    pub scan_seconds: f64,
    pub classify_seconds: f64,
    pub hash_calls: usize,
    pub process_seconds: f64,
    pub publish_seconds: f64,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct SourceResult {
    pub source_id: String,
    pub offline: Option<String>,
    pub plan: String,
    pub counts: DiffCounts,
    pub indexed: usize,
    pub retrying: usize,
    pub failed: usize,
    pub skipped_limit: usize,
    pub carried_forward: usize,
    pub build_id: Option<String>,
    pub published: bool,
    pub scan_errors: Vec<String>,
    pub timings: StageTimings,
}

#[derive(Debug, Clone)]
pub struct Options {
    pub workers: usize,
    pub max_file_size_bytes: i64,
    pub follow_symlinks: bool,
    pub cache_size_mb: i64,
    pub lock_timeout: Duration,
    /// Fault injection forwarded to the scanner (tests/drills only).
    pub inject_unreadable: Vec<PathBuf>,
}

impl Options {
    pub fn from_config(cfg: &RagMonkConfig) -> Self {
        let cpus = std::thread::available_parallelism().map_or(1, |n| n.get());
        let wanted = cfg
            .indexing
            .code_extraction_workers
            .max(cfg.indexing.document_extraction_workers)
            .max(1) as usize;
        Self {
            workers: wanted.min(cpus).max(1),
            max_file_size_bytes: cfg.indexing.max_file_size_mb.max(0) * 1024 * 1024,
            follow_symlinks: cfg.indexing.follow_symlinks,
            cache_size_mb: cfg.runtime.sqlite_cache_size_mb,
            lock_timeout: Duration::from_secs_f64(cfg.indexing.lock_timeout_seconds),
            inject_unreadable: Vec::new(),
        }
    }
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

fn db_err(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

struct Work {
    input: PrepareInput,
    prev_attempts: i64,
    size: i64,
    mtime: f64,
    skip_limit: bool,
}

struct Done {
    work: Work,
    outcome: Result<FileKnowledge, ProcessError>,
}

fn file_row(
    work: &Work,
    versions: &IndexVersions,
    status: &str,
    attempts: i64,
    next: Option<String>,
    err: Option<String>,
) -> FileRow {
    FileRow {
        id: work.input.file_id.clone(),
        rel_path: work.input.rel_path.clone(),
        kind: work.input.kind.as_str().into(),
        size: work.size,
        mtime: work.mtime,
        content_hash: work.input.content_hash.clone(),
        status: status.into(),
        parser_version: Some(versions.parser_version.clone()),
        chunker_version: Some(versions.chunker_version.clone()),
        converter_version: Some(versions.converter_version.clone()),
        embedding_model_id: versions.embedding_model_id.clone(),
        embedding_text_version: versions.embedding_text_version.clone(),
        last_error: err,
        attempt_count: attempts,
        next_attempt_at: next,
    }
}

fn run_workers(
    items: Vec<Work>,
    workers: usize,
    registry: &Registry,
    mut on_done: impl FnMut(Done) -> Result<(), RagMonkError>,
) -> Result<(), RagMonkError> {
    let bound = workers.saturating_mul(2).max(1);
    let (work_tx, work_rx) = sync_channel::<Work>(bound);
    let (done_tx, done_rx) = sync_channel::<Done>(bound);
    let work_rx: Arc<Mutex<Receiver<Work>>> = Arc::new(Mutex::new(work_rx));
    let cancel = AtomicBool::new(false);
    let mut first_err = None;
    std::thread::scope(|s| {
        for _ in 0..workers {
            let rx = Arc::clone(&work_rx);
            let tx = done_tx.clone();
            let cancel = &cancel;
            s.spawn(move || loop {
                let next = rx.lock().ok().and_then(|r| r.recv().ok());
                let Some(work) = next else { break };
                if cancel.load(Ordering::Relaxed) {
                    continue;
                }
                let outcome = if work.skip_limit {
                    Ok(FileKnowledge::default())
                } else {
                    let processor = registry.for_kind(work.input.kind);
                    catch_unwind(AssertUnwindSafe(|| processor.prepare(&work.input)))
                        .unwrap_or_else(|_| {
                            Err(ProcessError {
                                code: "processor_panic".into(),
                                message: "processor panicked".into(),
                                transient: false,
                            })
                        })
                };
                if tx.send(Done { work, outcome }).is_err() {
                    break;
                }
            });
        }
        drop(done_tx);
        let cancel_ref = &cancel;
        s.spawn(move || {
            for item in items {
                if cancel_ref.load(Ordering::Relaxed) || work_tx.send(item).is_err() {
                    break;
                }
            }
        });
        for done in done_rx {
            if first_err.is_some() {
                continue;
            }
            if let Err(e) = on_done(done) {
                cancel.store(true, Ordering::Relaxed);
                first_err = Some(e);
            }
        }
    });
    first_err.map_or(Ok(()), Err)
}

/// Indexes one source. Errors abort only this source's pending build.
pub fn run_source(
    layout: &V2Layout,
    control: &mut ControlPlane,
    source: &SourceRecord,
    registry: &Registry,
    opts: &Options,
    progress: &mut dyn Progress,
) -> Result<SourceResult, RagMonkError> {
    let mut result = SourceResult {
        source_id: source.id.clone(),
        ..SourceResult::default()
    };
    let root = Path::new(&source.path);
    if let Some(reason) = check_root_accessible(root) {
        control
            .set_online(&source.id, false, Some(&reason))
            .map_err(db_err)?;
        result.offline = Some(reason);
        return Ok(result);
    }
    control.set_online(&source.id, true, None).map_err(db_err)?;
    let state = control.state(&source.id).map_err(db_err)?;
    let plan = plan_for(&state, &registry.versions);
    result.plan = match &plan {
        RebuildPlan::Full { reason } => format!("full ({reason})"),
        RebuildPlan::Incremental { .. } => "incremental".into(),
    };
    let project_id = project_id_for_canonical(&source.path);
    let (mut store, _) =
        ProjectStore::open(layout, &project_id, &source.id, opts.cache_size_mb).map_err(db_err)?;

    progress.event(&ProgressEvent::Stage {
        source_id: source.id.clone(),
        stage: "scanning",
    });
    let started = Instant::now();
    let ignore = IgnoreMatcher::new(
        &ragmonk_core::paths::resolve(root).map_err(db_err)?,
        &source.exclude_patterns,
        &source.include_patterns,
    );
    let scanned = scan(
        root,
        &ignore,
        &ScanOptions {
            follow_symlinks: opts.follow_symlinks,
            inject_unreadable: opts.inject_unreadable.clone(),
        },
    )
    .map_err(|e| RagMonkError::new(ErrorKind::SourceUnavailable, format!("scan failed: {e}")))?;
    result.timings.scan_seconds = started.elapsed().as_secs_f64();
    result.scan_errors = scanned
        .errors
        .iter()
        .map(|e| format!("{}: {}", e.path, e.message))
        .collect();
    if !scanned.complete() {
        tracing::warn!(component = "indexing", event = "scan_incomplete", source_id = %source.id, errors = scanned.errors.len());
    }

    let started = Instant::now();
    let baseline = store.reusable_files(&plan).map_err(db_err)?;
    let now = now_iso();
    let d = diff(&scanned, &baseline, &now, &mut |p| hash_file(p));
    result.timings.classify_seconds = started.elapsed().as_secs_f64();
    result.timings.hash_calls = d.counts.hash_calls;
    result.counts = d.counts.clone();
    let to_process: Vec<&Decision> = d.to_process().collect();
    progress.event(&ProgressEvent::Scanned {
        source_id: source.id.clone(),
        files: d.counts.scanned,
        to_process: to_process.len(),
    });

    // Warm pass: nothing to rebuild. Refresh stale stats in place on the
    // active build (content proven identical by hash) and stop.
    if let RebuildPlan::Incremental { active_build_id } = &plan {
        if to_process.is_empty() && d.counts.deleted == 0 {
            for dec in d
                .decisions
                .iter()
                .filter(|x| x.change == Change::UnchangedRestat)
            {
                if let (Some(prev), Some(sf)) = (&dec.prev, &dec.scanned) {
                    store
                        .set_file_stat(active_build_id, &prev.id, sf.size, sf.mtime)
                        .map_err(db_err)?;
                }
            }
            result.build_id = Some(active_build_id.clone());
            return Ok(result);
        }
    }

    let build_id = v2::build_id(&source.id, &format!("{}-{}", now, std::process::id()));
    let full = matches!(plan, RebuildPlan::Full { .. });
    control.begin_build(&source.id, &build_id).map_err(db_err)?;
    result.build_id = Some(build_id.clone());
    let outcome = build(
        &mut store,
        &build_id,
        full,
        &plan,
        &d.decisions,
        &to_process,
        &source.id,
        registry,
        opts,
        progress,
        &mut result,
    );
    match outcome {
        Ok(()) => {
            let started = Instant::now();
            control
                .publish_build(&source.id, &build_id, &registry.versions, full)
                .map_err(db_err)?;
            store.mark_published(&build_id).map_err(db_err)?;
            store.gc_builds(Some(&build_id)).map_err(db_err)?;
            result.timings.publish_seconds = started.elapsed().as_secs_f64();
            result.published = true;
            Ok(result)
        }
        Err(e) => {
            let _ = control.abort_build(&source.id, &build_id, e.message());
            let _ = store.mark_aborted(&build_id);
            let _ = store.gc_builds(state.active_build_id.as_deref());
            Err(e)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn build(
    store: &mut ProjectStore,
    build_id: &str,
    full: bool,
    plan: &RebuildPlan,
    decisions: &[Decision],
    to_process: &[&Decision],
    source_id: &str,
    registry: &Registry,
    opts: &Options,
    progress: &mut dyn Progress,
    result: &mut SourceResult,
) -> Result<(), RagMonkError> {
    store
        .create_build(build_id, full, &registry.versions)
        .map_err(db_err)?;
    if let RebuildPlan::Incremental { active_build_id } = plan {
        // One set-based copy of the active build minus every file that is
        // reprocessed, deleted or moved away.
        let mut exclude = Vec::new();
        let mut restat = Vec::new();
        for dec in decisions {
            match dec.change {
                Change::Unchanged | Change::Unseen => result.carried_forward += 1,
                Change::UnchangedRestat => {
                    result.carried_forward += 1;
                    if let (Some(prev), Some(sf)) = (&dec.prev, &dec.scanned) {
                        restat.push((prev.id.clone(), sf.size, sf.mtime));
                    }
                }
                Change::Changed | Change::Deleted => {
                    if let Some(prev) = &dec.prev {
                        exclude.push(prev.id.clone());
                    }
                }
                Change::Moved => {
                    if let Some(old) = &dec.moved_from {
                        exclude.push(old.id.clone());
                    }
                }
                Change::New => {}
            }
        }
        store
            .carry_forward_except(active_build_id, build_id, &exclude, &restat)
            .map_err(db_err)?;
    }
    progress.event(&ProgressEvent::Stage {
        source_id: source_id.into(),
        stage: "processing",
    });
    let started = Instant::now();
    let total = to_process.len();
    let items: Vec<Work> = to_process
        .iter()
        .filter_map(|dec| {
            let sf = dec.scanned.as_ref()?;
            Some(Work {
                input: PrepareInput {
                    source_id: source_id.into(),
                    file_id: v2::file_id(source_id, &dec.rel_path),
                    rel_path: dec.rel_path.clone(),
                    path: sf.path.clone(),
                    kind: dec.kind,
                    content_hash: dec.content_hash.clone(),
                },
                prev_attempts: dec.prev.as_ref().map_or(0, |p| p.attempt_count),
                size: sf.size,
                mtime: sf.mtime,
                skip_limit: opts.max_file_size_bytes > 0 && sf.size > opts.max_file_size_bytes,
            })
        })
        .collect();
    let mut done = 0usize;
    run_workers(items, opts.workers, registry, |d| {
        let versions = &registry.versions;
        let (row, knowledge) = match d.outcome {
            Ok(_) if d.work.skip_limit => {
                result.skipped_limit += 1;
                (
                    file_row(&d.work, versions, "skipped_limit", 0, None, None),
                    FileKnowledge::default(),
                )
            }
            Ok(k) => {
                result.indexed += 1;
                (file_row(&d.work, versions, "indexed", 0, None, None), k)
            }
            Err(e) => {
                let attempts = d.work.prev_attempts + 1;
                let permanent = !e.transient || retry::is_permanent(attempts);
                let (status, next) = if permanent {
                    result.failed += 1;
                    ("failed", None)
                } else {
                    result.retrying += 1;
                    (
                        "retry",
                        Some(retry::next_attempt_at(attempts, chrono::Utc::now())),
                    )
                };
                store
                    .record_error(
                        Some(build_id),
                        Some(&d.work.input.file_id),
                        Some(&d.work.input.rel_path),
                        &e.code,
                        &e.message,
                    )
                    .map_err(db_err)?;
                (
                    file_row(&d.work, versions, status, attempts, next, Some(e.message)),
                    FileKnowledge::default(),
                )
            }
        };
        store.put_file(build_id, &row, &knowledge).map_err(db_err)?;
        done += 1;
        progress.event(&ProgressEvent::FileDone {
            source_id: source_id.into(),
            done,
            total,
        });
        Ok(())
    })?;
    result.timings.process_seconds = started.elapsed().as_secs_f64();
    Ok(())
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct RunSummary {
    pub sources: Vec<SourceResult>,
    /// `(source_id, error)` for sources whose pass failed.
    pub failures: Vec<(String, String)>,
}

/// Indexes every enabled source under the bounded `locks/index.lock`.
/// One source failing never stops the others.
pub fn index_all(
    home: &Home,
    control: &mut ControlPlane,
    registry: &Registry,
    opts: &Options,
    progress: &mut dyn Progress,
) -> Result<RunSummary, RagMonkError> {
    let lock = RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        "index",
        None,
        opts.lock_timeout,
    )?;
    let layout = V2Layout::new(home);
    let sources = control.list_sources(true).map_err(db_err)?;
    let mut summary = RunSummary::default();
    let total = sources.len();
    for (i, source) in sources.iter().enumerate() {
        progress.event(&ProgressEvent::SourceStarted {
            source_id: source.id.clone(),
            position: i + 1,
            total,
        });
        match run_source(&layout, control, source, registry, opts, progress) {
            Ok(r) => {
                progress.event(&ProgressEvent::SourceFinished {
                    source_id: source.id.clone(),
                    ok: true,
                });
                summary.sources.push(r);
            }
            Err(e) => {
                tracing::warn!(component = "indexing", event = "source_failed", source_id = %source.id, error = %e.message());
                progress.event(&ProgressEvent::SourceFinished {
                    source_id: source.id.clone(),
                    ok: false,
                });
                summary
                    .failures
                    .push((source.id.clone(), e.message().to_owned()));
            }
        }
    }
    lock.release();
    Ok(summary)
}
