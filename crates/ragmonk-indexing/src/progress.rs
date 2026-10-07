//! Live indexing progress snapshot (`<home>/index_progress.json`), ported
//! from the reference `service/progress.py`. `ragmonk status` reads it to
//! show which source and stage a run is on and whether it is still moving,
//! without talking to the indexing process.
//!
//! * The file format is the reference's (schema version 1, same keys), so
//!   either implementation reads the other's snapshot.
//! * Writes are atomic (temp file + rename) and coalesced: counter updates
//!   at most once per [`MIN_WRITE_INTERVAL`]; source/stage changes and the
//!   final snapshot are always written.
//! * The reader is tolerant: a missing, malformed or newer snapshot reads
//!   as `None`; unknown keys are ignored and wrongly typed known keys fall
//!   back to their defaults.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::coordinator::{FileOutcome, Progress, ProgressEvent};

pub const SCHEMA_VERSION: i64 = 1;
/// Minimum spacing of coalesced (counter-only) writes.
pub const MIN_WRITE_INTERVAL: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct IndexProgress {
    pub schema_version: i64,
    pub running: bool,
    pub pid: Option<i64>,
    pub operation: Option<String>,
    pub outcome: String,
    pub started_at: Option<String>,
    pub updated_at: Option<String>,
    pub completed_at: Option<String>,
    pub source_id: Option<String>,
    pub source_position: Option<i64>,
    pub source_total: Option<i64>,
    pub stage: Option<String>,
    pub scanned: i64,
    pub queued: i64,
    pub processing: i64,
    pub retry: i64,
    pub indexed: i64,
    pub failed: i64,
    pub error: Option<String>,
}

impl Default for IndexProgress {
    fn default() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            running: false,
            pid: None,
            operation: None,
            outcome: "running".into(),
            started_at: None,
            updated_at: None,
            completed_at: None,
            source_id: None,
            source_position: None,
            source_total: None,
            stage: None,
            scanned: 0,
            queued: 0,
            processing: 0,
            retry: 0,
            indexed: 0,
            failed: 0,
            error: None,
        }
    }
}

/// ISO-8601 UTC with microseconds and `+00:00`, like Python's
/// `datetime.now(UTC).isoformat()`.
pub fn now_iso() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
        .to_string()
}

/// Writes `progress` atomically to `path`.
pub fn write_progress(path: &Path, progress: &IndexProgress) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = PathBuf::from(format!(
        "{}.{}.{:?}.tmp",
        path.display(),
        std::process::id(),
        std::thread::current().id()
    ));
    std::fs::write(
        &tmp,
        serde_json::to_vec(progress).map_err(std::io::Error::other)?,
    )?;
    std::fs::rename(&tmp, path)
}

/// Reads a snapshot; `None` when missing, malformed or of a newer schema.
pub fn read_progress(path: &Path) -> Option<IndexProgress> {
    let data: Value = serde_json::from_slice(&std::fs::read(path).ok()?).ok()?;
    progress_from_value(&data)
}

/// Tolerant decoding of a snapshot object (see [`read_progress`]).
pub fn progress_from_value(data: &Value) -> Option<IndexProgress> {
    let obj = data.as_object()?;
    let version = obj.get("schema_version")?;
    if version.is_boolean() || version.as_i64()? > SCHEMA_VERSION {
        return None;
    }
    let int = |k: &str, d: i64| match obj.get(k) {
        Some(v) if !v.is_boolean() => v.as_i64().unwrap_or(d),
        _ => d,
    };
    let opt_int = |k: &str| match obj.get(k) {
        Some(v) if !v.is_boolean() => v.as_i64(),
        _ => None,
    };
    let opt_str = |k: &str| obj.get(k).and_then(Value::as_str).map(str::to_owned);
    Some(IndexProgress {
        schema_version: int("schema_version", SCHEMA_VERSION),
        running: obj.get("running").and_then(Value::as_bool).unwrap_or(false),
        pid: opt_int("pid"),
        operation: opt_str("operation"),
        outcome: opt_str("outcome").unwrap_or_else(|| "running".into()),
        started_at: opt_str("started_at"),
        updated_at: opt_str("updated_at"),
        completed_at: opt_str("completed_at"),
        source_id: opt_str("source_id"),
        source_position: opt_int("source_position"),
        source_total: opt_int("source_total"),
        stage: opt_str("stage"),
        scanned: int("scanned", 0),
        queued: int("queued", 0),
        processing: int("processing", 0),
        retry: int("retry", 0),
        indexed: int("indexed", 0),
        failed: int("failed", 0),
        error: opt_str("error"),
    })
}

struct Inner {
    path: PathBuf,
    progress: IndexProgress,
    last_write: Option<Instant>,
}

impl Inner {
    fn flush(&mut self, force: bool) {
        if !force
            && self
                .last_write
                .is_some_and(|t| t.elapsed() < MIN_WRITE_INTERVAL)
        {
            return;
        }
        self.progress.updated_at = Some(now_iso());
        self.last_write = Some(Instant::now());
        if let Err(e) = write_progress(&self.path, &self.progress) {
            tracing::debug!(component = "progress", event = "write_failed", error = %e);
        }
    }
}

/// Owns one run's snapshot and persists it. Cheap to clone; clones share
/// the snapshot (the coordinator's writer and a finalizer's heartbeat may
/// report from different call sites).
#[derive(Clone)]
pub struct ProgressTracker {
    inner: Arc<Mutex<Inner>>,
}

impl std::fmt::Debug for ProgressTracker {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ProgressTracker").finish_non_exhaustive()
    }
}

impl ProgressTracker {
    /// Starts tracking `operation` and writes the first snapshot.
    pub fn start(path: impl Into<PathBuf>, operation: &str, source_total: Option<i64>) -> Self {
        let now = now_iso();
        let mut inner = Inner {
            path: path.into(),
            progress: IndexProgress {
                running: true,
                pid: Some(i64::from(std::process::id())),
                operation: Some(operation.into()),
                started_at: Some(now.clone()),
                updated_at: Some(now),
                source_total,
                stage: Some("starting".into()),
                ..IndexProgress::default()
            },
            last_write: None,
        };
        inner.flush(true);
        Self {
            inner: Arc::new(Mutex::new(inner)),
        }
    }

    fn with<R>(&self, f: impl FnOnce(&mut Inner) -> R) -> R {
        let mut g = self.inner.lock().unwrap_or_else(|p| p.into_inner());
        f(&mut g)
    }

    /// The current snapshot.
    pub fn snapshot(&self) -> IndexProgress {
        self.with(|i| i.progress.clone())
    }

    pub fn begin_source(&self, source_id: &str, position: Option<i64>) {
        self.with(|i| {
            let p = &mut i.progress;
            p.source_id = Some(source_id.into());
            p.source_position = Some(position.unwrap_or(p.source_position.unwrap_or(0) + 1));
            p.stage = Some("scan".into());
            p.queued = 0;
            p.processing = 0;
            i.flush(true);
        });
    }

    pub fn stage(&self, name: &str) {
        self.with(|i| {
            if i.progress.stage.as_deref() != Some(name) {
                i.progress.stage = Some(name.into());
                i.flush(true);
            }
        });
    }

    pub fn scanned(&self, count: i64, queued: i64) {
        self.with(|i| {
            i.progress.scanned += count;
            i.progress.queued = queued;
            i.flush(false);
        });
    }

    /// Liveness signal from a long stage without per-file completions
    /// (linking, embeddings), coalesced like counter updates.
    pub fn heartbeat(&self) {
        self.with(|i| i.flush(false));
    }

    pub fn file_done(&self, outcome: FileOutcome) {
        self.with(|i| {
            let p = &mut i.progress;
            match outcome {
                FileOutcome::Indexed => p.indexed += 1,
                FileOutcome::Failed => p.failed += 1,
                FileOutcome::Retry => p.retry += 1,
                FileOutcome::Skipped => {}
            }
            if p.queued > 0 {
                p.queued -= 1;
            }
            i.flush(false);
        });
    }

    /// Final snapshot: `completed`, or `failed` with `error`.
    pub fn finish(&self, error: Option<&str>) {
        self.with(|i| {
            let p = &mut i.progress;
            p.running = false;
            p.outcome = if error.is_some() {
                "failed"
            } else {
                "completed"
            }
            .into();
            p.error = error.map(|e| e.chars().take(500).collect());
            p.completed_at = Some(now_iso());
            p.stage = Some("done".into());
            p.queued = 0;
            p.processing = 0;
            i.flush(true);
        });
    }
}

impl Progress for ProgressTracker {
    fn event(&mut self, event: &ProgressEvent) {
        match event {
            ProgressEvent::SourceStarted {
                source_id,
                position,
                ..
            } => {
                self.begin_source(source_id, Some(*position as i64));
            }
            ProgressEvent::Stage { stage, .. } => self.stage(stage),
            ProgressEvent::Scanned {
                files, to_process, ..
            } => {
                self.scanned(*files as i64, *to_process as i64);
            }
            ProgressEvent::FileDone { outcome, .. } => self.file_done(*outcome),
            ProgressEvent::Heartbeat => self.heartbeat(),
            ProgressEvent::SourceFinished { .. } => {}
        }
    }
}

static ACTIVE: Mutex<Option<ProgressTracker>> = Mutex::new(None);

/// Registers `tracker` as the process's active run, so long stages that
/// have no access to the coordinator's progress sink (build finalizers)
/// can still report liveness through [`heartbeat`]. The guard unregisters
/// it when dropped.
pub fn activate(tracker: &ProgressTracker) -> ActiveGuard {
    *ACTIVE.lock().unwrap_or_else(|p| p.into_inner()) = Some(tracker.clone());
    ActiveGuard(())
}

/// Drops the active-run registration.
#[derive(Debug)]
pub struct ActiveGuard(());

impl Drop for ActiveGuard {
    fn drop(&mut self) {
        *ACTIVE.lock().unwrap_or_else(|p| p.into_inner()) = None;
    }
}

/// Heartbeat for the active run, if any (a no-op otherwise).
pub fn heartbeat() {
    let active = ACTIVE.lock().unwrap_or_else(|p| p.into_inner()).clone();
    if let Some(t) = active {
        t.heartbeat();
    }
}

/// Runs `f` under a tracker for `operation`: the final snapshot is written
/// on success (`completed`) and on error (`failed: <message>`).
pub fn track<T, E: std::fmt::Display>(
    path: &Path,
    operation: &str,
    source_total: Option<i64>,
    f: impl FnOnce(&mut ProgressTracker) -> Result<T, E>,
) -> Result<T, E> {
    let mut tracker = ProgressTracker::start(path, operation, source_total);
    let _guard = activate(&tracker);
    let result = f(&mut tracker);
    match &result {
        Ok(_) => tracker.finish(None),
        Err(e) => tracker.finish(Some(&e.to_string())),
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_and_tolerates_bad_input() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("index_progress.json");
        assert!(read_progress(&path).is_none());
        let p = IndexProgress {
            running: true,
            pid: Some(7),
            stage: Some("process".into()),
            indexed: 3,
            ..IndexProgress::default()
        };
        write_progress(&path, &p).unwrap();
        assert_eq!(read_progress(&path).unwrap(), p);
        std::fs::write(&path, "not json").unwrap();
        assert!(read_progress(&path).is_none());
        std::fs::write(&path, r#"{"schema_version": 2}"#).unwrap();
        assert!(read_progress(&path).is_none());
        std::fs::write(&path, r#"{"schema_version": 1, "pid": true, "indexed": "x", "stage": 5, "extra": 1, "running": true}"#).unwrap();
        let r = read_progress(&path).unwrap();
        assert_eq!(
            (r.pid, r.indexed, r.stage, r.running),
            (None, 0, None, true)
        );
        let leftovers: Vec<_> = std::fs::read_dir(dir.path()).unwrap().collect();
        assert_eq!(leftovers.len(), 1, "no temp files left");
    }

    #[test]
    fn coalesces_counters_but_always_writes_stage_changes_and_finish() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        let t = ProgressTracker::start(&path, "index", Some(2));
        t.begin_source("s1", Some(1));
        for _ in 0..50 {
            t.file_done(FileOutcome::Indexed);
        }
        // Counter updates inside the interval are not yet on disk.
        assert_eq!(read_progress(&path).unwrap().indexed, 0);
        t.stage("publish");
        assert_eq!(read_progress(&path).unwrap().indexed, 50);
        t.file_done(FileOutcome::Retry);
        t.finish(None);
        let done = read_progress(&path).unwrap();
        assert_eq!(
            (
                done.running,
                done.outcome.as_str(),
                done.retry,
                done.stage.as_deref()
            ),
            (false, "completed", 1, Some("done"))
        );
    }

    #[test]
    fn track_records_failure_and_heartbeat_reaches_the_active_run() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        let r: Result<(), String> = track(&path, "rebuild", None, |_| {
            std::thread::sleep(MIN_WRITE_INTERVAL + Duration::from_millis(50));
            heartbeat();
            assert!(
                read_progress(&path).unwrap().updated_at > read_progress(&path).unwrap().started_at
            );
            Err("boom".into())
        });
        assert!(r.is_err());
        let p = read_progress(&path).unwrap();
        assert_eq!(
            (p.outcome.as_str(), p.error.as_deref()),
            ("failed", Some("boom"))
        );
        heartbeat(); // no active run: no-op
    }
}
