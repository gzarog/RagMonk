//! Run liveness: which source passes are running, on which host, and
//! whether they are still alive.
//!
//! * **Local mode**: this home's progress snapshot (one entry per source
//!   pass), its locks and a process probe. A pass is `live` while its
//!   process exists and its heartbeat is fresh; a snapshot that says
//!   running for a dead process is a crash.
//! * **Server mode**: `{prefix}-runtime` heartbeat documents of every
//!   host, checked against the source's writer lease (fencing token). A
//!   remote PID is never used as proof.

use chrono::{DateTime, Utc};

use crate::model::{LastRun, LiveRun, Liveness, LockInfo};
use crate::snapshot::{age_seconds, normalize_time, percentage};

/// Facts about this home's local runs.
#[derive(Debug, Clone, Default)]
pub struct LocalRuntime {
    pub runs: Vec<LiveRun>,
    pub lock: Option<LockInfo>,
    pub last_run: Option<LastRun>,
    pub max_parallel_sources: Option<u64>,
    pub resources: Option<serde_json::Value>,
}

fn count(v: i64) -> Option<u64> {
    u64::try_from(v).ok()
}

/// A lock as the report shows it.
pub fn lock_info(state: &ragmonk_indexing::lock::LockState) -> LockInfo {
    use ragmonk_indexing::lock::LockState;
    let (name, owner) = match state {
        LockState::Free => ("free", None),
        LockState::Held(o) => ("held", o.as_ref()),
        LockState::Unknown => ("unknown", None),
    };
    LockInfo {
        state: name.into(),
        pid: owner.and_then(|o| o.pid.as_deref()?.parse().ok()),
        operation: owner.and_then(|o| o.operation.clone()),
        hostname: owner.and_then(|o| o.hostname.clone()),
        source_id: owner.and_then(|o| o.source_id.clone()),
        acquired_at: owner.and_then(|o| normalize_time(o.acquired_at.as_deref())),
    }
}

/// Judges local runs from plain inputs (pure; `alive` probes a PID).
/// `source_locks` lists `(source_id, lock)` of sources whose lock is held.
pub fn local_runs(
    snapshot: Option<&ragmonk_indexing::progress::IndexProgress>,
    home_lock: LockInfo,
    source_locks: &[(String, LockInfo)],
    alive: &dyn Fn(i64) -> bool,
    threshold: f64,
    now: DateTime<Utc>,
) -> LocalRuntime {
    let mut out = LocalRuntime {
        lock: Some(home_lock),
        ..LocalRuntime::default()
    };
    if let Some(p) = snapshot {
        out.max_parallel_sources = p.max_parallel_sources.and_then(count);
        let process_alive = p.running && p.pid.is_some_and(alive);
        if p.running && process_alive {
            out.resources = p.resources.clone();
        }
        for s in &p.sources {
            let heartbeat = normalize_time(s.heartbeat_at.as_deref().or(p.updated_at.as_deref()));
            let age = age_seconds(heartbeat.as_deref(), now);
            let liveness = if !s.active {
                Liveness::Finished
            } else if !p.running || !process_alive {
                Liveness::Expired
            } else if age.is_some_and(|a| a > threshold) {
                Liveness::Stalled
            } else {
                Liveness::Live
            };
            let processed = count(s.processed);
            let planned = s.planned.and_then(count);
            out.runs.push(LiveRun {
                run_id: p.run_id.clone(),
                source_id: s.source_id.clone(),
                host: p.host.clone(),
                pid: p.pid,
                operation: p.operation.clone(),
                stage: s.stage.clone(),
                liveness,
                scanned: count(s.scanned),
                planned,
                processed,
                indexed: count(s.indexed),
                failed: count(s.failed),
                retry: count(s.retry),
                percentage: percentage(processed, planned),
                started_at: normalize_time(s.started_at.as_deref()),
                last_progress_at: normalize_time(s.last_progress_at.as_deref()),
                heartbeat_at: heartbeat,
                heartbeat_age_seconds: age,
                lease_token: None,
                lease_valid: None,
                outcome: s.outcome.clone(),
            });
        }
        if !p.running {
            out.last_run = Some(LastRun {
                run_id: p.run_id.clone(),
                operation: p.operation.clone(),
                outcome: p.outcome.clone(),
                started_at: normalize_time(p.started_at.as_deref()),
                completed_at: normalize_time(p.completed_at.as_deref()),
                error: p.error.clone(),
            });
        } else if !process_alive {
            out.last_run = Some(LastRun {
                run_id: p.run_id.clone(),
                operation: p.operation.clone(),
                outcome: "crashed".into(),
                started_at: normalize_time(p.started_at.as_deref()),
                completed_at: None,
                error: None,
            });
        }
    }
    // A held source lock without a live progress entry: another process
    // in this home runs a pass whose progress is not in this snapshot.
    for (source_id, lock) in source_locks {
        let shown = out
            .runs
            .iter()
            .any(|r| &r.source_id == source_id && r.liveness == Liveness::Live);
        if shown || lock.state != "held" {
            continue;
        }
        out.runs.retain(|r| &r.source_id != source_id);
        out.runs.push(LiveRun {
            run_id: None,
            source_id: source_id.clone(),
            host: lock.hostname.clone(),
            pid: lock.pid,
            operation: lock.operation.clone(),
            stage: None,
            liveness: Liveness::Live,
            scanned: None,
            planned: None,
            processed: None,
            indexed: None,
            failed: None,
            retry: None,
            percentage: None,
            started_at: lock.acquired_at.clone(),
            last_progress_at: None,
            heartbeat_at: None,
            heartbeat_age_seconds: None,
            lease_token: None,
            lease_valid: None,
            outcome: None,
        });
    }
    out
}

/// [`local_runs`] over the real files of `home`.
pub fn read_local(
    home: &ragmonk_core::paths::Home,
    source_ids: &[String],
    threshold: f64,
    now: DateTime<Utc>,
) -> LocalRuntime {
    use ragmonk_indexing::lock::{inspect_lock, source_lock_path};
    let snapshot = ragmonk_indexing::progress::read_progress(&home.index_progress());
    let home_lock = lock_info(&inspect_lock(&home.locks_dir().join("index.lock")));
    let source_locks: Vec<(String, LockInfo)> = source_ids
        .iter()
        .filter_map(|id| {
            let path = source_lock_path(home, id);
            path.exists()
                .then(|| (id.clone(), lock_info(&inspect_lock(&path))))
        })
        .collect();
    local_runs(
        snapshot.as_ref(),
        home_lock,
        &source_locks,
        &ragmonk_indexing::runtime::is_process_alive,
        threshold,
        now,
    )
}
