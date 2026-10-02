//! Coordinator behavior: publication, warm no-op, incomplete/offline
//! safety, retries, panic isolation, source isolation, locks and scale.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use ragmonk_core::paths::project_id_for_canonical;
use ragmonk_indexing::coordinator::{
    index_all, run_source, NoProgress, Options, PrepareInput, ProcessError, Processor, Progress,
    ProgressEvent, Registry,
};
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::control::BuildState;
use ragmonk_storage::knowledge::{FileKnowledge, ProjectStore};
use ragmonk_storage::V2Layout;

fn opts() -> Options {
    let mut o = Options::from_config(&ragmonk_config::RagMonkConfig::default());
    o.workers = 4;
    o
}

fn visible_files(
    layout: &V2Layout,
    cp: &ragmonk_storage::control::ControlPlane,
    src: &ragmonk_storage::control::SourceRecord,
) -> Vec<(String, String)> {
    let state = cp.state(&src.id).unwrap();
    let Some(active) = state.active_build_id else {
        return vec![];
    };
    let (store, _) =
        ProjectStore::open(layout, &project_id_for_canonical(&src.path), &src.id, 8).unwrap();
    store
        .files(&active)
        .unwrap()
        .into_iter()
        .map(|f| (f.rel_path, f.status))
        .collect()
}

#[test]
fn full_then_warm_then_incremental() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    for i in 0..50 {
        common::write(&root, &format!("pkg/m{i}.py"), &format!("x = {i}\n"));
    }
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let reg = Registry::raw();

    let r = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert!(r.plan.starts_with("full"));
    assert!(r.published);
    assert_eq!((r.counts.new, r.indexed), (50, 50));
    assert_eq!(cp.state(&src.id).unwrap().build_state, BuildState::Ready);

    let warm = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert_eq!(warm.plan, "incremental");
    assert!(!warm.published, "no-op pass creates no build");
    assert_eq!((warm.counts.unchanged, warm.timings.hash_calls), (50, 0));

    common::write(&root, "pkg/m3.py", "x = 'edited'\n");
    std::fs::remove_file(root.join("pkg/m4.py")).unwrap();
    let inc = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert!(inc.published);
    assert_eq!(
        (
            inc.counts.changed,
            inc.counts.deleted,
            inc.carried_forward,
            inc.indexed
        ),
        (1, 1, 48, 1)
    );
    assert_eq!(visible_files(&layout, &cp, &src).len(), 49);
}

#[test]
fn incomplete_scan_keeps_unseen_files_and_offline_root_keeps_build() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    common::write(&root, "a.py", "a");
    common::write(&root, "sub/b.py", "b");
    common::write(&root, "sub/c.py", "c");
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let reg = Registry::raw();
    run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();

    common::write(&root, "a.py", "a2");
    let mut o = opts();
    o.inject_unreadable = vec![root.join("sub")];
    let r = run_source(&layout, &mut cp, &src, &reg, &o, &mut NoProgress).unwrap();
    assert_eq!(
        (r.counts.deleted, r.counts.unseen_kept, r.counts.changed),
        (0, 2, 1)
    );
    assert!(!r.scan_errors.is_empty());
    assert_eq!(
        visible_files(&layout, &cp, &src).len(),
        3,
        "unreadable subtree never looks deleted"
    );

    // Offline root: nothing is touched.
    std::fs::rename(&root, tmp.path().join("moved-away")).unwrap();
    let before = cp.state(&src.id).unwrap().active_build_id;
    let off = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert!(off.offline.is_some());
    assert_eq!(cp.state(&src.id).unwrap().active_build_id, before);
    assert_eq!(cp.state(&src.id).unwrap().online_status, "offline");
    assert_eq!(visible_files(&layout, &cp, &src).len(), 3);
}

/// Fails `fail.py` (transient or not) and panics on `panic.py`.
struct Flaky {
    transient: bool,
    calls: AtomicUsize,
}

impl Processor for Flaky {
    fn prepare(&self, input: &PrepareInput) -> Result<FileKnowledge, ProcessError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if input.rel_path == "panic.py" {
            panic!("boom");
        }
        if input.rel_path == "fail.py" {
            return Err(ProcessError {
                code: "E".into(),
                message: "nope".into(),
                transient: self.transient,
            });
        }
        Ok(FileKnowledge::default())
    }
}

#[test]
fn failures_are_isolated_per_file_and_retried_with_backoff() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    for f in ["ok.py", "fail.py", "panic.py"] {
        common::write(&root, f, f);
    }
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let flaky = Arc::new(Flaky {
        transient: true,
        calls: AtomicUsize::new(0),
    });
    let mut reg = Registry::raw();
    reg.code = flaky.clone();

    let r = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert!(r.published, "file failures never block publication");
    assert_eq!((r.indexed, r.retrying, r.failed), (1, 1, 1));
    let files = visible_files(&layout, &cp, &src);
    assert!(files.contains(&("fail.py".into(), "retry".into())));
    assert!(files.contains(&("panic.py".into(), "failed".into())));

    // Not due yet: the retrying file is carried forward, not reprocessed.
    let calls = flaky.calls.load(Ordering::SeqCst);
    let warm = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert_eq!(warm.counts.changed, 0);
    assert_eq!(flaky.calls.load(Ordering::SeqCst), calls);
}

#[test]
fn permanent_failures_stop_retrying() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    common::write(&root, "fail.py", "x");
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let mut reg = Registry::raw();
    reg.code = Arc::new(Flaky {
        transient: false,
        calls: AtomicUsize::new(0),
    });
    let r = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert_eq!((r.failed, r.retrying), (1, 0));
    let warm = run_source(&layout, &mut cp, &src, &reg, &opts(), &mut NoProgress).unwrap();
    assert_eq!(
        warm.counts.changed, 0,
        "failed files wait for a content change"
    );
}

#[test]
fn oversized_files_are_recorded_not_processed() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("r");
    common::write(&root, "big.py", &"x".repeat(2048));
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    let mut o = opts();
    o.max_file_size_bytes = 1024;
    let r = run_source(
        &layout,
        &mut cp,
        &src,
        &Registry::raw(),
        &o,
        &mut NoProgress,
    )
    .unwrap();
    assert_eq!(r.skipped_limit, 1);
    assert_eq!(
        visible_files(&layout, &cp, &src),
        vec![("big.py".into(), "skipped_limit".into())]
    );
}

struct Collect(Vec<ProgressEvent>);
impl Progress for Collect {
    fn event(&mut self, e: &ProgressEvent) {
        self.0.push(e.clone());
    }
}

#[test]
fn one_failing_source_does_not_stop_others_and_lock_is_bounded() {
    let tmp = tempfile::tempdir().unwrap();
    let home = common::home(tmp.path());
    let mut cp = common::control(&home);
    let good1 = tmp.path().join("g1");
    let bad = tmp.path().join("bad");
    let good2 = tmp.path().join("g2");
    common::write(&good1, "a.py", "a");
    common::write(&bad, "b.py", "b");
    common::write(&good2, "c.py", "c");
    common::add_source(&mut cp, &good1, &[], &[]);
    let bad_src = common::add_source(&mut cp, &bad, &[], &[]);
    common::add_source(&mut cp, &good2, &[], &[]);
    // Break the bad source's V2 project store: its path is a file.
    let layout = V2Layout::new(&home);
    let pdir = layout.project_dir(&project_id_for_canonical(&bad_src.path));
    std::fs::create_dir_all(pdir.parent().unwrap()).unwrap();
    std::fs::write(&pdir, "not a directory").unwrap();

    let mut events = Collect(vec![]);
    let summary = index_all(&home, &mut cp, &Registry::raw(), &opts(), &mut events).unwrap();
    assert_eq!(summary.sources.len(), 2);
    assert_eq!(summary.failures.len(), 1);
    assert_eq!(summary.failures[0].0, bad_src.id);
    assert!(events
        .0
        .iter()
        .any(|e| matches!(e, ProgressEvent::SourceFinished { ok: false, .. })));
    assert!(events
        .0
        .iter()
        .any(|e| matches!(e, ProgressEvent::FileDone { .. })));

    // Lock contention is bounded and diagnosable.
    let held = RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        "daemon",
        None,
        Duration::from_secs(1),
    )
    .unwrap();
    let mut o = opts();
    o.lock_timeout = Duration::from_millis(300);
    let started = Instant::now();
    let err = index_all(&home, &mut cp, &Registry::raw(), &o, &mut NoProgress).unwrap_err();
    assert!(started.elapsed() < Duration::from_secs(3));
    assert!(
        err.message().contains(if cfg!(windows) {
            "owner unknown"
        } else {
            "operation=daemon"
        }),
        "{}",
        err.message()
    );
    drop(held);
}

#[test]
fn many_sources_make_progress_with_bounded_work() {
    let tmp = tempfile::tempdir().unwrap();
    let home = common::home(tmp.path());
    let mut cp = common::control(&home);
    let n_sources = 150;
    for s in 0..n_sources {
        let root = tmp.path().join(format!("repo{s:03}"));
        for f in 0..5 {
            common::write(&root, &format!("src/f{f}.py"), &format!("v = {s}{f}\n"));
        }
        common::add_source(&mut cp, &root, &[], &[]);
    }
    let started = Instant::now();
    let summary = index_all(&home, &mut cp, &Registry::raw(), &opts(), &mut NoProgress).unwrap();
    let cold = started.elapsed();
    assert!(summary.failures.is_empty());
    assert_eq!(summary.sources.len(), n_sources);
    assert!(summary
        .sources
        .iter()
        .all(|s| s.published && s.indexed == 5));
    let started = Instant::now();
    let warm = index_all(&home, &mut cp, &Registry::raw(), &opts(), &mut NoProgress).unwrap();
    let warm_t = started.elapsed();
    assert!(warm
        .sources
        .iter()
        .all(|s| !s.published && s.counts.unchanged == 5));
    eprintln!("150 sources x 5 files: cold {cold:?}, warm {warm_t:?}");
}
