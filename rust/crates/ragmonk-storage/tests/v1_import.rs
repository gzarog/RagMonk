//! Upgrade from a real Python-era home (`rust/compat/fixtures/v1_home`,
//! produced by `rust/compat/tools/gen_v1_home.py`).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_storage::control::{plan_for, BuildState, ControlPlane, IndexVersions, RebuildPlan};
use ragmonk_storage::knowledge::{FileKnowledge, FileRow, ProjectStore};
use ragmonk_storage::preflight::{import_sources, preflight};
use ragmonk_storage::V2Layout;

fn fixture() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../compat/fixtures/v1_home")
}

fn copy_dir(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).unwrap();
    for entry in std::fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let dest = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_dir(&entry.path(), &dest);
        } else {
            std::fs::copy(entry.path(), dest).unwrap();
        }
    }
}

fn snapshot(dir: &Path) -> BTreeMap<PathBuf, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![dir.to_path_buf()];
    while let Some(d) = stack.pop() {
        for entry in std::fs::read_dir(&d).unwrap() {
            let p = entry.unwrap().path();
            if p.is_dir() {
                if p.file_name().is_some_and(|n| n == "v2") {
                    continue;
                }
                stack.push(p);
            } else {
                out.insert(
                    p.strip_prefix(dir).unwrap().to_path_buf(),
                    std::fs::read(&p).unwrap(),
                );
            }
        }
    }
    out
}

fn versions() -> IndexVersions {
    IndexVersions {
        schema_version: ragmonk_storage::schema::V2_SCHEMA_VERSION,
        parser_version: "p".into(),
        chunker_version: "c".into(),
        converter_version: "d".into(),
        embedding_model_id: None,
        embedding_text_version: None,
    }
}

fn home() -> (tempfile::TempDir, Home) {
    let tmp = tempfile::tempdir().unwrap();
    copy_dir(&fixture(), &tmp.path().join("home"));
    let home = Home::new(tmp.path().join("home"));
    (tmp, home)
}

#[test]
fn preflight_is_read_only_and_reports_python_state() {
    let (_tmp, home) = home();
    let before = snapshot(home.root());
    let report = preflight(&home).unwrap();
    assert!(report.v1_sources_db_present);
    assert_eq!(report.v1_sources_schema_version, Some(2));
    assert!(!report.v2_control_present);
    assert_eq!(report.sources.len(), 2);
    for s in &report.sources {
        assert!(!s.path_exists, "fixture source dirs no longer exist");
        assert!(!s.already_imported);
        assert!(s.ignored_v1_state.db_present);
        let files = s
            .ignored_v1_state
            .tables
            .iter()
            .find(|(t, _)| t == "files")
            .unwrap()
            .1;
        assert!(files > 0, "Python indexed this source");
    }
    let disabled: Vec<_> = report.sources.iter().filter(|s| !s.enabled).collect();
    assert_eq!(disabled.len(), 1);
    assert_eq!(disabled[0].v2_action, "full_rebuild_when_enabled");
    assert_eq!(snapshot(home.root()), before, "preflight must not write");
    assert!(!home.root().join("v2").exists());
}

#[test]
fn import_preserves_definitions_and_forces_full_v2_rebuild() {
    let (_tmp, home) = home();
    let before = snapshot(home.root());
    let v1 = ragmonk_storage::v1::read_sources(&home).unwrap().unwrap().1;

    let first = import_sources(&home, 8).unwrap();
    assert_eq!(first.imported.len(), 2);
    assert!(
        first.control_backup.is_none(),
        "fresh control DB needs no backup"
    );
    let again = import_sources(&home, 8).unwrap();
    assert!(again.imported.is_empty());
    assert_eq!(again.already_present.len(), 2);

    let layout = V2Layout::new(&home);
    let (cp, _) = ControlPlane::open(&layout, 8).unwrap();
    let sources = cp.list_sources(false).unwrap();
    assert_eq!(sources.len(), 2);
    for (old, new) in v1.iter().zip(&sources) {
        assert_eq!(new.id, old.id, "V2 keeps the path-derived source id");
        assert_eq!(new.path, old.path);
        assert_eq!(new.enabled, old.enabled);
        assert_eq!(new.include_patterns, old.include_patterns);
        assert_eq!(new.exclude_patterns, old.exclude_patterns);
        assert_eq!(new.created_at, old.created_at);
        let state = cp.state(&new.id).unwrap();
        assert_eq!(state.build_state, BuildState::NeedsFullRebuild);
        assert!(state.rebuild_reason.unwrap().contains("Python V1"));
        assert_eq!(state.active_build_id, None);
        assert!(matches!(
            plan_for(&cp.state(&new.id).unwrap(), &versions()),
            RebuildPlan::Full { .. }
        ));

        // No V1 file state is visible to V2: the V2 project store is empty
        // and a full plan never offers reusable files.
        let project_id = project_id_for_canonical(&new.path);
        let (store, _) = ProjectStore::open(&layout, &project_id, &new.id, 8).unwrap();
        let plan = plan_for(&cp.state(&new.id).unwrap(), &versions());
        assert!(store.reusable_files(&plan).unwrap().is_empty());
        let n: i64 = store
            .connection()
            .query_row("SELECT COUNT(*) FROM files", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 0);
    }
    assert!(old_patterns_present(&v1));
    assert_eq!(
        snapshot(home.root()),
        before,
        "Python V1 files must be untouched"
    );
}

fn old_patterns_present(v1: &[ragmonk_storage::v1::V1Source]) -> bool {
    v1.iter().any(|s| !s.include_patterns.is_empty())
        && v1.iter().any(|s| !s.exclude_patterns.is_empty())
}

#[test]
fn unpublished_v2_state_never_suppresses_a_full_rebuild() {
    let (_tmp, home) = home();
    import_sources(&home, 8).unwrap();
    let layout = V2Layout::new(&home);
    let (mut cp, _) = ControlPlane::open(&layout, 8).unwrap();
    let src = cp.list_sources(true).unwrap().remove(0);
    let project_id = project_id_for_canonical(&src.path);
    let (mut store, _) = ProjectStore::open(&layout, &project_id, &src.id, 8).unwrap();

    // A crashed first build left rows behind...
    cp.begin_build(&src.id, "crashed").unwrap();
    store.create_build("crashed", true, &versions()).unwrap();
    let file = FileRow {
        id: ragmonk_core::ids::v2::file_id(&src.id, "a.py"),
        rel_path: "a.py".into(),
        kind: "code".into(),
        size: 1,
        mtime: 1.0,
        content_hash: Some("h".into()),
        status: "indexed".into(),
        parser_version: Some("p".into()),
        chunker_version: None,
        converter_version: None,
        embedding_model_id: None,
        embedding_text_version: None,
        last_error: None,
    };
    store
        .put_file("crashed", &file, &FileKnowledge::default())
        .unwrap();
    // ...but the source is still not published, so the next pass is full
    // and reuses nothing.
    let plan = plan_for(&cp.state(&src.id).unwrap(), &versions());
    assert!(matches!(plan, RebuildPlan::Full { .. }));
    assert!(store.reusable_files(&plan).unwrap().is_empty());
    assert_eq!(store.recover_stuck().unwrap(), 0);
}
