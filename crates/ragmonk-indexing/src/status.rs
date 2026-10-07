//! Indexing status verdicts: the indexer state (running / stalled /
//! crashed / idle), per-source index states, the problem list and the
//! overall health.
//!
//! The verdicts are pure functions over plain inputs (lock view, progress
//! snapshot, a process-liveness probe, the clock) and build the
//! documented JSON shapes, so the CLI, MCP and admin UI render one
//! source of truth. [`indexer_state`] and [`recent_errors`] are the I/O
//! wrappers.

use chrono::{DateTime, Utc};
use ragmonk_core::errors::LockOwner;
use serde_json::{json, Map, Value};

use crate::lock::{inspect_lock, LockState};
use crate::progress::{read_progress, IndexProgress};

/// Per-source state precedence: the first match wins.
pub const INDEX_STATE_PRECEDENCE: [&str; 8] = [
    "offline",
    "stalled",
    "indexing",
    "retrying",
    "errors",
    "waiting",
    "completed",
    "idle",
];

/// What the index lock looks like from outside.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockView {
    /// `free`, `held` or `unknown`.
    pub state: &'static str,
    pub owner: Option<LockOwner>,
}

impl From<LockState> for LockView {
    fn from(s: LockState) -> Self {
        match s {
            LockState::Free => Self {
                state: "free",
                owner: None,
            },
            LockState::Held(owner) => Self {
                state: "held",
                owner,
            },
            LockState::Unknown => Self {
                state: "unknown",
                owner: None,
            },
        }
    }
}

fn parse_iso(v: Option<&str>) -> Option<DateTime<Utc>> {
    let v = v?;
    DateTime::parse_from_rfc3339(v)
        .map(|d| d.with_timezone(&Utc))
        .ok()
        .or_else(|| {
            chrono::NaiveDateTime::parse_from_str(v, "%Y-%m-%dT%H:%M:%S%.f")
                .ok()
                .map(|n| n.and_utc())
        })
}

/// Seconds since `v`, never negative, rounded to 0.1.
fn age_seconds(v: Option<&str>, now: DateTime<Utc>) -> Option<f64> {
    let t = parse_iso(v)?;
    let secs = (now - t)
        .num_microseconds()
        .map_or(0.0, |us| us as f64 / 1e6)
        .max(0.0);
    Some((secs * 10.0).round() / 10.0)
}

fn owner_pid(owner: Option<&LockOwner>) -> Option<i64> {
    owner
        .and_then(|o| o.pid.as_deref())
        .and_then(|p| p.parse().ok())
}

/// Merges lock ownership and the progress snapshot into one verdict:
/// `running` (lock held, or a live process's snapshot says a run is in
/// flight, with a fresh heartbeat), `stalled` (heartbeat older than
/// `threshold` seconds), `crashed` (the snapshot says running but the lock
/// is free and its process is gone) or `idle`.
pub fn indexer_state_from(
    lock: &LockView,
    snapshot: Option<&IndexProgress>,
    alive: impl Fn(i64) -> bool,
    threshold: f64,
    now: DateTime<Utc>,
) -> Value {
    let owner = lock.owner.as_ref();
    let held = lock.state == "held";
    let mut info = json!({
        "state": "idle",
        "lock_state": lock.state,
        "pid": null,
        "operation": null,
        "hostname": owner.and_then(|o| o.hostname.clone()),
        "lock_source_id": owner.and_then(|o| o.source_id.clone()),
        "acquired_at": owner.and_then(|o| o.acquired_at.clone()),
        "running_for_seconds": null,
        "current_source_id": null,
        "source_position": null,
        "source_total": null,
        "stage": null,
        "started_at": null,
        "last_activity_at": null,
        "last_activity_age_seconds": null,
        "stall_threshold_seconds": threshold,
        "stall_reason": null,
        "run": null,
        "last_run": null,
    });
    if held {
        info["pid"] = json!(owner_pid(owner));
        info["operation"] = json!(owner.and_then(|o| o.operation.clone()));
    }
    let mut live = false;
    if let Some(s) = snapshot {
        let mut run = json!({
            "operation": s.operation, "outcome": s.outcome, "pid": s.pid,
            "started_at": s.started_at, "updated_at": s.updated_at,
            "completed_at": s.completed_at, "scanned": s.scanned, "queued": s.queued,
            "retry": s.retry, "indexed": s.indexed, "failed": s.failed, "error": s.error,
        });
        if s.running {
            let pid_alive = s.pid.is_some_and(&alive);
            let same_owner = held && (owner_pid(owner).is_none() || owner_pid(owner) == s.pid);
            live = same_owner || pid_alive;
            if live {
                info["run"] = run;
            } else {
                run["outcome"] = json!("crashed");
                info["last_run"] = run;
            }
        } else {
            info["last_run"] = run;
        }
    }
    if let (true, Some(s)) = (live, snapshot) {
        if info["pid"].is_null() {
            info["pid"] = json!(s.pid);
        }
        if info["operation"].is_null() {
            info["operation"] = json!(s.operation);
        }
        info["current_source_id"] = json!(s.source_id);
        info["source_position"] = json!(s.source_position);
        info["source_total"] = json!(s.source_total);
        info["stage"] = json!(s.stage);
        info["started_at"] = json!(s.started_at);
        info["last_activity_at"] = json!(s.updated_at);
    } else if held {
        info["current_source_id"] = json!(owner.and_then(|o| o.source_id.clone()));
        info["started_at"] = json!(owner.and_then(|o| o.acquired_at.clone()));
    }
    info["last_activity_age_seconds"] = json!(age_seconds(info["last_activity_at"].as_str(), now));
    let running_since = info["acquired_at"]
        .as_str()
        .or(info["started_at"].as_str())
        .map(str::to_owned);
    info["running_for_seconds"] = json!(age_seconds(running_since.as_deref(), now));
    if held || live {
        match info["last_activity_age_seconds"].as_f64() {
            Some(age) if age > threshold => {
                info["state"] = json!("stalled");
                info["stall_reason"] = json!(format!("no progress heartbeat for {}s", age as i64));
            }
            _ => info["state"] = json!("running"),
        }
    } else if info["last_run"]["outcome"] == "crashed" {
        info["state"] = json!("crashed");
    }
    info
}

/// Whether process `pid` exists (safe, cross-platform).
pub fn is_process_alive(pid: i64) -> bool {
    let Ok(pid) = u32::try_from(pid) else {
        return false;
    };
    if pid == std::process::id() {
        return true;
    }
    let pid = sysinfo::Pid::from_u32(pid);
    let mut sys = sysinfo::System::new();
    sys.refresh_processes(sysinfo::ProcessesToUpdate::Some(&[pid]), true);
    // An exited-but-unreaped child is a zombie: not running.
    sys.process(pid)
        .is_some_and(|p| p.status() != sysinfo::ProcessStatus::Zombie)
}

/// [`indexer_state_from`] over the real lock file and progress snapshot.
pub fn indexer_state(
    home: &ragmonk_core::paths::Home,
    threshold: f64,
    now: DateTime<Utc>,
) -> Value {
    let lock = LockView::from(inspect_lock(&home.locks_dir().join("index.lock")));
    let snapshot = read_progress(&home.index_progress());
    indexer_state_from(&lock, snapshot.as_ref(), is_process_alive, threshold, now)
}

/// Per-source index state; the first match in [`INDEX_STATE_PRECEDENCE`]
/// wins. `access_state` (root reachability) is an input, never a synonym.
pub fn derive_index_state(
    access_state: &str,
    queue: &Value,
    failed_files: i64,
    last_error: Option<&str>,
    last_scan_at: Option<&str>,
    indexer: &Value,
    source_id: &str,
) -> &'static str {
    let current = indexer["current_source_id"].as_str() == Some(source_id);
    let q = |k: &str| queue[k].as_i64().unwrap_or(0);
    if access_state == "offline" {
        "offline"
    } else if current && indexer["state"] == "stalled" {
        "stalled"
    } else if current && indexer["state"] == "running" {
        "indexing"
    } else if q("retry") > 0 {
        "retrying"
    } else if failed_files > 0 || q("failed") > 0 || last_error.is_some_and(|e| !e.is_empty()) {
        "errors"
    } else if q("queued") > 0 || q("processing") > 0 {
        "waiting"
    } else if last_scan_at.is_some_and(|s| !s.is_empty()) {
        "completed"
    } else {
        "idle"
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// A value as message text (`-` for null).
fn text(v: &Value) -> String {
    match v {
        Value::Null => "-".into(),
        Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Problems explaining the health verdict. Severities: `error` (work
/// cannot continue), `warning` (degraded), `info`; scopes: `file`,
/// `source`, `backend`, `runtime`.
pub fn derive_problems(
    sources: &[Value],
    indexer: &Value,
    backend: &Value,
    queue: &Value,
) -> Vec<Value> {
    let mut out = Vec::new();
    let mut add = |sev: &str, scope: &str, message: String, source_id: Value| {
        out.push(
            json!({"severity": sev, "scope": scope, "source_id": source_id, "message": message}),
        );
    };
    if truthy(&backend["error"]) {
        add("error", "backend", text(&backend["error"]), Value::Null);
    }
    if indexer["state"] == "stalled" {
        add(
            "error",
            "runtime",
            format!("indexing stalled: {}", text(&indexer["stall_reason"])),
            indexer["current_source_id"].clone(),
        );
    }
    let last_run = if indexer["last_run"].is_object() {
        indexer["last_run"].clone()
    } else {
        json!({})
    };
    let op = || {
        let o = &last_run["operation"];
        if truthy(o) {
            text(o)
        } else {
            "index".into()
        }
    };
    if indexer["state"] == "crashed" {
        add(
            "error",
            "runtime",
            format!(
                "last {} run (pid {}) exited without finishing",
                op(),
                text(&last_run["pid"])
            ),
            Value::Null,
        );
    } else if last_run["outcome"] == "failed" && indexer["state"] == "idle" {
        let error = if truthy(&last_run["error"]) {
            text(&last_run["error"])
        } else {
            String::new()
        };
        let sev = if error.starts_with("KeyboardInterrupt") {
            "warning"
        } else {
            "error"
        };
        add(
            sev,
            "runtime",
            format!("last {} run failed: {error}", op()),
            Value::Null,
        );
    }
    let enabled: Vec<&Value> = sources.iter().filter(|r| truthy(&r["enabled"])).collect();
    let offline: Vec<&&Value> = enabled
        .iter()
        .filter(|r| r["access_state"] == "offline")
        .collect();
    for r in &offline {
        let reason = if truthy(&r["last_error"]) {
            text(&r["last_error"])
        } else {
            "unreachable".into()
        };
        add(
            "warning",
            "source",
            format!("source offline: {reason}"),
            r["id"].clone(),
        );
    }
    if !enabled.is_empty() && offline.len() == enabled.len() {
        add(
            "error",
            "source",
            "all enabled sources are offline".into(),
            Value::Null,
        );
    }
    for r in sources {
        if r["access_state"] == "offline" {
            continue;
        }
        if truthy(&r["last_error"]) {
            add("warning", "source", text(&r["last_error"]), r["id"].clone());
        }
        let failed = r["counts"]["failed"].as_i64().unwrap_or(0);
        if failed != 0 {
            add(
                "warning",
                "file",
                format!("{failed} file(s) failed"),
                r["id"].clone(),
            );
        }
        let retry = r["queue"]["retry"].as_i64().unwrap_or(0);
        if retry != 0 {
            let latest = &r["queue"]["latest_job_error"];
            let detail = if truthy(latest) {
                format!(
                    " (last: {}: {})",
                    text(&latest["error_code"]),
                    text(&latest["error_message"])
                )
            } else {
                String::new()
            };
            add(
                "warning",
                "file",
                format!("{retry} job(s) awaiting retry{detail}"),
                r["id"].clone(),
            );
        }
    }
    let depth = queue["depth"].as_i64().unwrap_or(0);
    if depth != 0 && (indexer["state"] == "idle" || indexer["state"] == "crashed") {
        add(
            "info",
            "runtime",
            format!("{depth} job(s) pending with no indexer running (run 'ragmonk index' or start the daemon)"),
            Value::Null,
        );
    }
    out
}

/// `failed` on any error, `degraded` on any warning, else `healthy`.
pub fn derive_health(problems: &[Value]) -> &'static str {
    let has = |s: &str| problems.iter().any(|p| p["severity"] == s);
    if has("error") {
        "failed"
    } else if has("warning") {
        "degraded"
    } else {
        "healthy"
    }
}

/// Sums per-source queue stats (earliest timestamps, max attempts, first
/// latest error).
pub fn merge_queue_stats(stats: &[Value]) -> Value {
    let mut m: Map<String, Value> = Map::new();
    for k in [
        "queued",
        "processing",
        "retry",
        "failed",
        "completed",
        "depth",
    ] {
        m.insert(
            k.into(),
            json!(stats
                .iter()
                .map(|s| s[k].as_i64().unwrap_or(0))
                .sum::<i64>()),
        );
    }
    for k in [
        "oldest_pending_created_at",
        "oldest_processing_started_at",
        "next_retry_at",
    ] {
        let min = stats
            .iter()
            .filter_map(|s| s[k].as_str().filter(|v| !v.is_empty()))
            .min();
        m.insert(k.into(), json!(min));
    }
    m.insert(
        "max_attempt_count".into(),
        json!(stats
            .iter()
            .map(|s| s["max_attempt_count"].as_i64().unwrap_or(0))
            .max()
            .unwrap_or(0)),
    );
    m.insert(
        "latest_job_error".into(),
        stats
            .iter()
            .map(|s| &s["latest_job_error"])
            .find(|e| truthy(e))
            .cloned()
            .unwrap_or(Value::Null),
    );
    Value::Object(m)
}

// ---- full report -------------------------------------------------------

/// Recent errors kept in the report by default.
pub const DEFAULT_RECENT_ERRORS: i64 = 10;

fn file_size(p: &std::path::Path) -> u64 {
    std::fs::metadata(p).map_or(0, |m| m.len())
}

fn access_state(enabled: bool, online_status: &str) -> &'static str {
    if !enabled {
        "disabled"
    } else if online_status == "offline" {
        "offline"
    } else {
        "online"
    }
}

/// One source's report row plus its recent errors (newest first).
fn source_row(
    layout: &ragmonk_storage::StorageLayout,
    control: &ragmonk_storage::control::ControlPlane,
    source: &ragmonk_storage::control::SourceRecord,
    indexer: &Value,
    error_limit: i64,
) -> Result<(Value, Vec<Value>), ragmonk_storage::error::StorageError> {
    use ragmonk_storage::knowledge::ProjectStore;
    let state = control.state(&source.id)?;
    let project_id = ragmonk_core::paths::project_id_for_canonical(&source.path);
    let db = layout.project_db(&project_id);
    let mut counts = Map::new();
    let mut queue = json!({
        "queued": 0, "processing": 0, "retry": 0, "failed": 0, "completed": 0, "depth": 0,
        "oldest_pending_created_at": null, "oldest_processing_started_at": null,
        "next_retry_at": null, "max_attempt_count": 0, "latest_job_error": null,
    });
    let (mut symbols, mut relationships, mut documents) = (0, 0, 0);
    let mut last_error_detail = Value::Null;
    let mut errors = Vec::new();
    if db.exists() {
        let store = ProjectStore::open(layout, &project_id, &source.id, 8)?;
        if let Some(build) = state.active_build_id.as_deref() {
            for (k, v) in store.file_counts_by_status(build)? {
                counts.insert(k, json!(v));
            }
            let r = store.retry_stats(build)?;
            queue["retry"] = json!(r.retry);
            queue["failed"] = json!(r.failed);
            queue["completed"] = json!(r.completed);
            queue["depth"] = json!(r.retry);
            queue["next_retry_at"] = json!(r.next_retry_at);
            queue["max_attempt_count"] = json!(r.max_attempt_count);
            queue["latest_job_error"] = json!(store.latest_retry_error(build)?);
            symbols = store.count("entities", build)?;
            relationships = store.count("relationships", build)?;
            documents = store.count("documents", build)?;
        }
        let recent = store.recent_errors(error_limit)?;
        last_error_detail = recent.first().map_or(Value::Null, |e| json!(e));
        errors = recent
            .into_iter()
            .map(|e| {
                json!({"source_id": source.id, "path": e.path, "error_code": e.error_code,
                       "error_message": e.error_message, "occurred_at": e.occurred_at})
            })
            .collect();
    }
    let access = access_state(source.enabled, &state.online_status);
    let failed_files = counts.get("failed").and_then(Value::as_i64).unwrap_or(0);
    let index_state = derive_index_state(
        access,
        &queue,
        failed_files,
        state.last_error.as_deref(),
        state.last_scan_at.as_deref(),
        indexer,
        &source.id,
    );
    let last_activity = if indexer["current_source_id"].as_str() == Some(&source.id)
        && truthy(&indexer["last_activity_at"])
    {
        indexer["last_activity_at"].clone()
    } else {
        json!(state.last_scan_at)
    };
    let row = json!({
        "id": source.id,
        "path": source.path,
        "type": serde_json::to_value(source.source_type).unwrap_or(Value::Null),
        "enabled": source.enabled,
        "status": state.online_status,
        "counts": counts,
        "queue_depth": queue["depth"],
        "last_scan_at": state.last_scan_at,
        "last_error": state.last_error,
        "access_state": access,
        "index_state": index_state,
        "queue": queue,
        "last_activity_at": last_activity,
        "last_error_detail": last_error_detail,
        "metrics": {
            "symbols_created": symbols,
            "relationships_created": relationships,
            "documents_processed": documents,
            "database_size_bytes": file_size(&db),
        },
    });
    Ok((row, errors))
}

fn source_has_errors(row: &Value) -> bool {
    truthy(&row["last_error"])
        || truthy(&row["counts"]["failed"])
        || truthy(&row["queue"]["failed"])
        || truthy(&row["queue"]["retry"])
        || row["access_state"] == "offline"
}

/// The full status payload for a home (local mode):
/// health, indexer verdict, merged queue, recent errors, problems,
/// per-source rows and totals. The CLI adds the tokenizer and backend
/// sections it renders.
pub fn collect_status(
    home: &ragmonk_core::paths::Home,
    control: &ragmonk_storage::control::ControlPlane,
    threshold: f64,
    now: DateTime<Utc>,
) -> Result<Value, ragmonk_storage::error::StorageError> {
    let layout = ragmonk_storage::StorageLayout::new(home);
    let indexer = indexer_state(home, threshold, now);
    let mut rows = Vec::new();
    let mut errors = Vec::new();
    for source in control.list_sources(false)? {
        let (row, errs) = source_row(&layout, control, &source, &indexer, DEFAULT_RECENT_ERRORS)?;
        rows.push(row);
        errors.extend(errs);
    }
    errors.sort_by(|a, b| b["occurred_at"].as_str().cmp(&a["occurred_at"].as_str()));
    errors.truncate(DEFAULT_RECENT_ERRORS as usize);
    let queues: Vec<Value> = rows.iter().map(|r| r["queue"].clone()).collect();
    let queue = merge_queue_stats(&queues);
    let backend = json!({"type": "local"});
    let problems = derive_problems(&rows, &indexer, &backend, &queue);
    let mut by_status: Map<String, Value> = Map::new();
    let sum = |k: &str| -> i64 {
        rows.iter()
            .map(|r| r["metrics"][k].as_i64().unwrap_or(0))
            .sum()
    };
    let symbols = sum("symbols_created");
    let relationships = sum("relationships_created");
    let documents = sum("documents_processed");
    let db_bytes = sum("database_size_bytes") + file_size(&layout.control_db()) as i64;
    for r in &rows {
        for (k, v) in r["counts"].as_object().into_iter().flatten() {
            let cur = by_status.get(k).and_then(Value::as_i64).unwrap_or(0);
            by_status.insert(k.clone(), json!(cur + v.as_i64().unwrap_or(0)));
        }
    }
    let total_files: i64 = by_status.values().filter_map(Value::as_i64).sum();
    let get = |k: &str| by_status.get(k).and_then(Value::as_i64).unwrap_or(0);
    let depth: i64 = rows
        .iter()
        .map(|r| r["queue_depth"].as_i64().unwrap_or(0))
        .sum();
    Ok(json!({
        "health": {"status": derive_health(&problems), "problem_count": problems.len()},
        "indexer": indexer,
        "queue": queue,
        "recent_errors": errors,
        "recent_error_count": errors.len(),
        "sources_with_errors": rows.iter().filter(|r| source_has_errors(r)).count(),
        "problems": problems,
        "sources": rows,
        "backend": backend,
        "totals": {
            "by_status": by_status.clone(),
            "queue_depth": depth,
            "metrics": {
                "files_discovered": total_files,
                "files_indexed": get("indexed"),
                "files_failed": get("failed"),
                "symbols_created": symbols,
                "relationships_created": relationships,
                "documents_processed": documents,
                "index_queue_depth": depth,
                "database_size_bytes": db_bytes,
            },
        },
    }))
}
