//! Status observability end to end: a tracked run writes the progress
//! snapshot, and `collect_status` reports counts, retry state, recent
//! errors, problems and the indexer verdict (running / stalled / crashed).

mod common;

use std::time::Duration;

use chrono::Utc;
use ragmonk_indexing::coordinator::{
    index_all, Options, PrepareInput, ProcessError, Processor, Registry,
};
use ragmonk_indexing::lock::RunLock;
use ragmonk_indexing::progress::{read_progress, track, write_progress, IndexProgress};
use ragmonk_indexing::status::{collect_status, indexer_state};
use ragmonk_storage::knowledge::FileKnowledge;

/// `retry.py` fails transiently, `bad.py` permanently.
struct Flaky;

impl Processor for Flaky {
    fn prepare(&self, input: &PrepareInput) -> Result<FileKnowledge, ProcessError> {
        match input.rel_path.as_str() {
            "retry.py" => Err(ProcessError {
                code: "io_busy".into(),
                message: "file busy".into(),
                transient: true,
            }),
            "bad.py" => Err(ProcessError {
                code: "parse".into(),
                message: "syntax".into(),
                transient: false,
            }),
            _ => Ok(FileKnowledge::default()),
        }
    }
}

#[test]
fn tracked_run_feeds_progress_and_status_report() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    for f in ["ok.py", "retry.py", "bad.py"] {
        common::write(&root, f, f);
    }
    let home = common::home(tmp.path());
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let mut reg = Registry::raw();
    reg.code = std::sync::Arc::new(Flaky);
    let opts = Options::from_config(&ragmonk_config::RagMonkConfig::default());

    track(&home.index_progress(), "index", Some(1), |tracker| {
        index_all(&home, &mut cp, &reg, &opts, tracker).map(|_| ())
    })
    .unwrap();

    let p = read_progress(&home.index_progress()).unwrap();
    assert!(!p.running);
    assert_eq!(
        (p.outcome.as_str(), p.stage.as_deref()),
        ("completed", Some("done"))
    );
    assert_eq!((p.indexed, p.retry, p.failed, p.scanned), (1, 1, 1, 3));
    assert_eq!(p.source_id.as_deref(), Some(src.id.as_str()));

    let s = collect_status(&home, &cp, 120.0, Utc::now()).unwrap();
    assert_eq!(s["indexer"]["state"], "idle");
    assert_eq!(s["indexer"]["last_run"]["outcome"], "completed");
    let row = &s["sources"][0];
    assert_eq!(row["counts"]["indexed"], 1);
    assert_eq!(row["counts"]["retry"], 1);
    assert_eq!(row["counts"]["failed"], 1);
    assert_eq!(row["index_state"], "retrying");
    assert_eq!(row["queue"]["latest_job_error"]["error_code"], "io_busy");
    assert!(row["queue"]["next_retry_at"].is_string());
    assert_eq!(row["queue"]["max_attempt_count"], 1);
    let messages: Vec<&str> = s["problems"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p["message"].as_str().unwrap())
        .collect();
    assert!(messages.contains(&"1 file(s) failed"), "{messages:?}");
    assert!(
        messages.contains(&"1 job(s) awaiting retry (last: io_busy: file busy)"),
        "{messages:?}"
    );
    assert_eq!(s["health"]["status"], "degraded");
    assert_eq!(s["recent_error_count"], 2);
    assert_eq!(s["sources_with_errors"], 1);
    assert_eq!(s["totals"]["metrics"]["files_discovered"], 3);
}

/// A pid that existed and has exited.
fn dead_pid() -> i64 {
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .arg("--list")
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let pid = i64::from(child.id());
    child.wait().unwrap();
    pid
}

#[test]
fn indexer_verdicts_from_real_lock_and_snapshot() {
    let tmp = tempfile::tempdir().unwrap();
    let home = common::home(tmp.path());
    let path = home.index_progress();
    let now = Utc::now();
    let iso = |d: chrono::DateTime<Utc>| d.format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string();
    assert_eq!(indexer_state(&home, 120.0, now)["state"], "idle");

    // Live run: our own pid, fresh heartbeat.
    let mut p = IndexProgress {
        running: true,
        pid: Some(i64::from(std::process::id())),
        operation: Some("index".into()),
        updated_at: Some(iso(now)),
        stage: Some("process".into()),
        ..IndexProgress::default()
    };
    write_progress(&path, &p).unwrap();
    assert_eq!(indexer_state(&home, 120.0, now)["state"], "running");

    // Stale heartbeat while the lock is held: stalled.
    p.updated_at = Some(iso(now - chrono::Duration::seconds(600)));
    write_progress(&path, &p).unwrap();
    let lock = RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        "index",
        None,
        Duration::from_secs(1),
    )
    .unwrap();
    let st = indexer_state(&home, 120.0, now);
    assert_eq!(st["state"], "stalled");
    assert_eq!(st["stall_reason"], "no progress heartbeat for 600s");
    drop(lock);

    // Snapshot says running but its process is gone and the lock is free.
    p.pid = Some(dead_pid());
    write_progress(&path, &p).unwrap();
    let st = indexer_state(&home, 120.0, now);
    assert_eq!(st["state"], "crashed");
    assert_eq!(st["last_run"]["outcome"], "crashed");
}
