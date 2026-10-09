//! The two-stage run (post-index relationships): every selected source is
//! indexed and published before any relationship graph is built; graph
//! failures are reported separately and never retract a published index;
//! `relationships build` retries graphs without reindexing; disabling
//! relationships skips the stage and hides graphs.

use std::path::{Path, PathBuf};

use ragmonk_config::RagMonkConfig;
use ragmonk_core::errors::ErrorKind;
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_service::indexing::{index_sources, selected_sources, SourceEvent};
use ragmonk_service::relationships::RelationshipOutcome;
use ragmonk_storage::graph::GraphLifecycle;
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::StorageLayout;

fn write(root: &Path, rel: &str, body: &str) {
    let p = root.join(rel);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, body).unwrap();
}

struct Env {
    _d: tempfile::TempDir,
    home: Home,
    roots: Vec<PathBuf>,
}

fn setup(relationships: bool) -> Env {
    let d = tempfile::tempdir().unwrap();
    let home = Home::new(d.path().join("home"));
    home.ensure_layout().unwrap();
    let mut cfg = RagMonkConfig::default();
    cfg.updates.enabled = false;
    cfg.indexing.relationships_enabled = relationships;
    ragmonk_config::write_user_config(&cfg, &home).unwrap();
    let mut roots = Vec::new();
    let mut c = ragmonk_service::sources::catalog(&home).unwrap();
    for name in ["alpha", "beta", "gamma"] {
        let r = d.path().join(name);
        write(
            &r,
            "app/main.py",
            "from app import util\n\ndef main():\n    return util.helper()\n",
        );
        write(&r, "app/util.py", "def helper():\n    return 1\n");
        write(&r, "docs/guide.md", "# Guide\n\nCall helper from main.\n");
        c.add(r.to_str().unwrap(), vec![], vec![]).unwrap();
        roots.push(r);
    }
    Env { _d: d, home, roots }
}

fn set_relationships(env: &Env, on: bool) {
    let mut cfg = ragmonk_service::load(&env.home).unwrap();
    cfg.indexing.relationships_enabled = on;
    ragmonk_config::write_user_config(&cfg, &env.home).unwrap();
}

/// `(source_id, phase)` in the order the run reported them.
fn run(env: &Env) -> (Vec<(String, &'static str)>, Result<(), ErrorKind>) {
    let sources = selected_sources(&env.home, None).unwrap();
    let mut events = Vec::new();
    let r = index_sources(&env.home, &sources, "index", |e| match e {
        SourceEvent::Completed { source, run } => {
            assert!(run.offline.is_none());
            events.push((source.id.clone(), "indexed"))
        }
        SourceEvent::Relationships { source, outcome } => {
            events.push((source.id.clone(), outcome.state()))
        }
        SourceEvent::Failed { source, error } | SourceEvent::Blocked { source, error } => {
            panic!("{}: {}", source.id, error.message())
        }
        SourceEvent::Started { .. } => {}
    })
    .unwrap();
    (events, r.into_result().map(drop).map_err(|e| e.kind()))
}

fn store(env: &Env, i: usize) -> (ProjectStore, String) {
    let sources = selected_sources(&env.home, None).unwrap();
    let s = sources
        .iter()
        .find(|s| Path::new(&s.path) == ragmonk_core::paths::resolve(&env.roots[i]).unwrap())
        .unwrap();
    let cp = ragmonk_service::sources::control_plane(&env.home).unwrap();
    let active = cp.state(&s.id).unwrap().active_build_id.unwrap();
    let store = ProjectStore::open(
        &StorageLayout::new(&env.home),
        &project_id_for_canonical(&s.path),
        &s.id,
        8,
    )
    .unwrap();
    (store, active)
}

#[test]
fn every_source_is_indexed_before_any_relationship_graph_is_built() {
    let env = setup(true);
    let (events, r) = run(&env);
    assert_eq!(r, Ok(()));
    let first_graph = events
        .iter()
        .position(|(_, p)| *p != "indexed")
        .expect("graph stage ran");
    assert_eq!(first_graph, 3, "barrier violated: {events:?}");
    assert!(events[3..].iter().all(|(_, p)| *p == "ready"), "{events:?}");
    for i in 0..3 {
        let (s, b) = store(&env, i);
        assert!(s.graph_visible(&b).unwrap());
        assert!(s.count("relationships", &b).unwrap() > 0);
    }

    // A warm pass reindexes nothing and finds every graph current.
    let (events, r) = run(&env);
    assert_eq!(r, Ok(()));
    assert!(
        events[3..].iter().all(|(_, p)| *p == "up_to_date"),
        "{events:?}"
    );
}

#[test]
fn a_graph_failure_is_a_partial_success_that_keeps_the_index_and_can_be_retried() {
    let env = setup(true);
    let sources = selected_sources(&env.home, None).unwrap();
    let edited = ragmonk_core::paths::resolve(&env.roots[0]).unwrap();
    let mut outcomes = Vec::new();
    // Edit one source's file after its index was published but before the
    // graph stage (the barrier guarantees that order).
    let summary = index_sources(&env.home, &sources, "index", |e| match e {
        SourceEvent::Completed { source, .. } if Path::new(&source.path) == edited => write(
            &env.roots[0],
            "app/util.py",
            "def helper():\n    return 2\n",
        ),
        SourceEvent::Relationships { source, outcome } => {
            outcomes.push((source.path.clone(), outcome.state()));
            if Path::new(&source.path) == edited {
                assert!(
                    matches!(outcome, RelationshipOutcome::Stale(_)),
                    "{outcome:?}"
                );
            }
        }
        _ => {}
    })
    .unwrap();
    assert_eq!(summary.failed_sources, 0);
    assert_eq!(summary.failed_files, 0);
    assert_eq!(summary.relationships.stale, 1);
    assert_eq!(summary.relationships.built, 2);
    assert_eq!(summary.outcome(), "partial_success");
    let err = summary.into_result().unwrap_err();
    assert_eq!(err.kind(), ErrorKind::IndexingPartialFailure);
    assert!(
        err.message().contains("relationships were not published"),
        "{}",
        err.message()
    );

    // The index of the edited source is published and searchable; its
    // graph is not visible and nothing of it was written.
    let (s, b) = store(&env, 0);
    assert!(s.count("entities", &b).unwrap() > 0);
    assert!(!s.search_code(&b, "helper", 5).unwrap().is_empty());
    assert!(!s.graph_visible(&b).unwrap());
    assert_eq!(s.count("relationships", &b).unwrap(), 0);
    assert_eq!(
        s.graph_status(Some(&b)).unwrap().state,
        GraphLifecycle::Stale
    );
    drop(s);

    // A graph-only retry still refuses the mismatched snapshot...
    let only = vec![sources
        .iter()
        .find(|s| Path::new(&s.path) == edited)
        .unwrap()
        .clone()];
    let r = ragmonk_service::relationships::build(&env.home, &only, |_, _| {}).unwrap();
    assert_eq!((r.stale, r.built), (1, 0));
    // ...and succeeds, without reindexing, once the index caught up.
    let (_, r) = run(&env);
    assert_eq!(r, Ok(()));
    let r = ragmonk_service::relationships::build(&env.home, &only, |_, _| {}).unwrap();
    assert_eq!((r.up_to_date, r.not_current()), (1, 0));
    let (s, b) = store(&env, 0);
    assert!(s.graph_visible(&b).unwrap());
}

#[test]
fn disabled_relationships_skip_the_stage_and_hide_graphs_without_reindexing() {
    let env = setup(true);
    assert_eq!(run(&env).1, Ok(()));
    let (s, b) = store(&env, 1);
    let parser = s.files(&b).unwrap()[0].parser_version.clone();
    drop(s);

    set_relationships(&env, false);
    let (events, r) = run(&env);
    assert_eq!(r, Ok(()));
    assert!(events.iter().all(|(_, p)| *p == "indexed"), "{events:?}");
    let (s, b2) = store(&env, 1);
    assert_eq!(b2, b, "toggling relationships never forces a rebuild");
    assert_eq!(s.files(&b2).unwrap()[0].parser_version, parser);
    assert!(!s.graph_visible(&b2).unwrap());
    assert_eq!(
        s.graph_status(Some(&b2)).unwrap().state,
        GraphLifecycle::Disabled
    );
    // Graph readers see nothing; regular search still works.
    assert!(s.entity_links(&b2, "x").unwrap().is_empty());
    assert!(!s.search_code(&b2, "helper", 5).unwrap().is_empty());
    drop(s);
    assert!(ragmonk_service::relationships::build(
        &env.home,
        &selected_sources(&env.home, None).unwrap(),
        |_, _| {}
    )
    .is_err());

    // Re-enabled: the retained graph is re-validated, not reindexed.
    set_relationships(&env, true);
    let (events, r) = run(&env);
    assert_eq!(r, Ok(()));
    assert!(events[3..].iter().all(|(_, p)| *p == "ready"), "{events:?}");
    let (s, b3) = store(&env, 1);
    assert_eq!(b3, b);
    assert!(s.graph_visible(&b3).unwrap());
}

#[test]
fn a_disabled_fresh_home_never_builds_a_graph() {
    let env = setup(false);
    let (events, r) = run(&env);
    assert_eq!(r, Ok(()));
    assert_eq!(events.len(), 3, "{events:?}");
    for i in 0..3 {
        let (s, b) = store(&env, i);
        assert_eq!(s.count("relationships", &b).unwrap(), 0);
        assert_eq!(s.count("cross_links", &b).unwrap(), 0);
        assert!(s.count("entities", &b).unwrap() > 0);
    }
}
