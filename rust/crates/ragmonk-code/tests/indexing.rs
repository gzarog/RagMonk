//! Code intelligence through the V2 indexer: parse-failure isolation,
//! whole-build resolution equal to the reference semantics, incremental
//! re-resolution, determinism and graph traversal.

mod common;

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use ragmonk_code::graph::{traverse_symbol, Direction};
use ragmonk_code::process::{entity_ids, prepare_source, PreparedCode};
use ragmonk_core::ids::v2;
use ragmonk_core::paths::project_id_for_canonical;
use ragmonk_indexing::coordinator::{run_source, NoProgress, Options};
use ragmonk_storage::control::{ControlPlane, SourceRecord};
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::V2Layout;
use serde_json::{json, Value};

fn compat() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../compat")
}

fn copy_tree(from: &Path, to: &Path) {
    for e in std::fs::read_dir(from).unwrap() {
        let p = e.unwrap().path();
        let dest = to.join(p.file_name().unwrap());
        if p.is_dir() {
            std::fs::create_dir_all(&dest).unwrap();
            copy_tree(&p, &dest);
        } else {
            let text = std::fs::read(&p).unwrap();
            let text = String::from_utf8_lossy(&text).replace("\r\n", "\n");
            std::fs::write(dest, text).unwrap();
        }
    }
}

fn opts() -> Options {
    let mut o = Options::from_config(&ragmonk_config::RagMonkConfig::default());
    o.workers = 4;
    o
}

struct Fixture {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    layout: V2Layout,
    cp: ControlPlane,
    src: SourceRecord,
}

fn fixture() -> Fixture {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("corpus");
    std::fs::create_dir_all(&root).unwrap();
    copy_tree(&compat().join("fixtures/code"), &root);
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    Fixture {
        _tmp: tmp,
        root,
        layout,
        cp,
        src,
    }
}

impl Fixture {
    fn index(&mut self) -> ragmonk_indexing::coordinator::SourceResult {
        run_source(
            &self.layout,
            &mut self.cp,
            &self.src,
            &ragmonk_code::registry(),
            &opts(),
            &mut NoProgress,
        )
        .unwrap()
    }

    fn store(&self) -> (ProjectStore, String) {
        let active = self
            .cp
            .state(&self.src.id)
            .unwrap()
            .active_build_id
            .unwrap();
        let (store, _) = ProjectStore::open(
            &self.layout,
            &project_id_for_canonical(&self.src.path),
            &self.src.id,
            8,
        )
        .unwrap();
        (store, active)
    }

    fn count(&self, sql: &str) -> i64 {
        let (store, active) = self.store();
        store
            .connection()
            .query_row(sql, [&active], |r| r.get(0))
            .unwrap()
    }
}

/// Order-independent sort key: objects serialized with sorted keys (the
/// workspace may enable serde_json's `preserve_order`).
fn sort_key(v: &Value) -> String {
    fn sorted(v: &Value) -> Value {
        match v {
            Value::Object(m) => {
                let b: std::collections::BTreeMap<_, _> =
                    m.iter().map(|(k, x)| (k.clone(), sorted(x))).collect();
                Value::Object(b.into_iter().collect())
            }
            Value::Array(a) => Value::Array(a.iter().map(sorted).collect()),
            x => x.clone(),
        }
    }
    serde_json::to_string(&sorted(v)).unwrap()
}

#[test]
fn cold_build_matches_reference_semantics_and_isolates_parse_failures() {
    let mut f = fixture();
    let r = f.index();
    assert!(r.published, "{r:?}");
    assert_eq!(r.failed, 1, "only broken.py fails");
    let (store, active) = f.store();
    let files = store.files(&active).unwrap();
    let status: HashMap<_, _> = files
        .iter()
        .map(|x| (x.rel_path.clone(), x.status.clone()))
        .collect();
    assert_eq!(status["python/broken.py"], "failed");
    assert_eq!(status["python/animals.py"], "indexed");
    assert_eq!(status["misc/script.rb"], "indexed");

    // Translate V2 ids back to the reference generator's "<rel>#<local>" keys.
    let mut key_of: HashMap<String, String> = HashMap::new();
    for file in &files {
        let source = std::fs::read(f.root.join(&file.rel_path)).unwrap();
        if let Ok(PreparedCode::Parsed { extraction, .. }) = prepare_source(&source, &file.rel_path)
        {
            assert_eq!(file.id, v2::file_id(&f.src.id, &file.rel_path));
            for (i, id) in entity_ids(&file.id, &extraction).into_iter().enumerate() {
                key_of.insert(id, format!("{}#{i}", file.rel_path));
            }
        }
    }
    let golden: Value =
        serde_json::from_str(&std::fs::read_to_string(compat().join("golden/code.json")).unwrap())
            .unwrap();
    let mut mismatches = Vec::new();
    for file in &files {
        let want = &golden["files"][&file.rel_path]["relationships"];
        let Some(want) = want.as_array() else {
            continue;
        };
        let mut want = want.clone();
        let mut got: Vec<Value> = store
            .file_relationships(&active, &file.id)
            .unwrap()
            .into_iter()
            .map(|r| {
                json!({
                    "type": r.relationship_type,
                    "source": key_of[&r.source_entity_id],
                    "target": r.target_entity_id.as_ref().map(|t| key_of[t].clone()),
                    "symbol": r.target_symbol, "resolver": r.resolver,
                    "confidence": r.confidence, "location": r.source_location,
                    "evidence": r.evidence,
                })
            })
            .collect();
        let key = |v: &Value| sort_key(v);
        got.sort_by_key(key);
        want.sort_by_key(key);
        if got != want {
            mismatches.push(format!(
                "{}:\n rust   {got:?}\n python {want:?}",
                file.rel_path
            ));
        }
    }
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}

#[test]
fn incremental_builds_re_resolve_cross_file_references() {
    let mut f = fixture();
    f.index();
    let unresolved = "SELECT COUNT(*) FROM relationships WHERE build_id = ?1
        AND reference_text = 'unknown_function' AND target_entity_id IS NULL";
    assert_eq!(f.count(unresolved), 1);

    // A new file defines the symbol; the unchanged caller now resolves.
    common::write(
        &f.root,
        "python/pkg/late.py",
        "def unknown_function():\n    return 0\n",
    );
    let r = f.index();
    assert!(r.published);
    assert_eq!(f.count(unresolved), 0);
    let resolved = "SELECT COUNT(*) FROM relationships r JOIN entities e
        ON e.build_id = r.build_id AND e.id = r.target_entity_id
        WHERE r.build_id = ?1 AND r.reference_text = 'unknown_function'
          AND r.resolver = 'name_only'";
    assert_eq!(f.count(resolved), 1);

    // Deleting the target leaves no dangling edge anywhere.
    std::fs::remove_file(f.root.join("python/pkg/late.py")).unwrap();
    std::fs::remove_file(f.root.join("python/pkg/helpers.py")).unwrap();
    assert!(f.index().published);
    let dangling = "SELECT COUNT(*) FROM relationships r WHERE r.build_id = ?1
        AND r.target_entity_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM entities e WHERE e.build_id = r.build_id AND e.id = r.target_entity_id)";
    assert_eq!(f.count(dangling), 0);
    assert_eq!(f.count(unresolved), 1);
    let pending = "SELECT COUNT(*) FROM relationships WHERE build_id = ?1 AND resolver = 'pending'";
    assert_eq!(f.count(pending), 0);
}

#[test]
fn rebuilding_from_scratch_reproduces_identical_ids() {
    fn dump(f: &Fixture) -> BTreeMap<String, String> {
        let (store, active) = f.store();
        let mut out = BTreeMap::new();
        for file in store.files(&active).unwrap() {
            for r in store.file_relationships(&active, &file.id).unwrap() {
                out.insert(r.id.clone(), format!("{r:?}"));
            }
            for e in store.file_entities(&active, &file.id).unwrap() {
                out.insert(e.id.clone(), format!("{e:?}"));
            }
        }
        out
    }
    let mut f = fixture();
    f.index();
    let first = dump(&f);
    assert!(first.len() > 300, "{}", first.len());

    // Rebuild the same source from scratch in a fresh, empty home (open
    // handles keep Windows from deleting the first one).
    let home_root = f
        .layout
        .root()
        .parent()
        .unwrap()
        .with_file_name("home-again");
    let home = ragmonk_core::paths::Home::new(&home_root);
    f.layout = V2Layout::new(&home);
    f.cp = common::control(&home);
    let src = common::add_source(&mut f.cp, &f.root.clone(), &[], &[]);
    assert_eq!(src.id, f.src.id);
    f.src = src;
    f.index();
    assert_eq!(dump(&f), first);
}

#[test]
fn callers_and_callees_traverse_the_build() {
    let mut f = fixture();
    f.index();
    let (store, active) = f.store();
    let (matches, edges) = traverse_symbol(
        &store,
        &active,
        "python.animals.Dog.bark",
        Direction::Incoming,
        &["calls"],
        1,
        100,
    )
    .unwrap();
    assert_eq!(matches.len(), 1);
    let evidence: Vec<_> = edges
        .iter()
        .map(|e| e.relationship.evidence.clone().unwrap())
        .collect();
    assert!(evidence.contains(&"d.bark".to_owned()), "{evidence:?}");
    // `dog.bark` in another file resolves by bare name to the first `bark`
    // by qualified name (Java's), exactly as the reference does.
    assert!(!evidence.contains(&"dog.bark".to_owned()), "{evidence:?}");

    let (_, out) = traverse_symbol(
        &store,
        &active,
        "python.pkg.service.Kennel",
        Direction::Outgoing,
        &[],
        2,
        100,
    )
    .unwrap();
    assert!(
        out.iter().any(|e| e.depth == 2),
        "two hops reach method bodies"
    );
    assert!(out.windows(2).all(|w| w[0].depth <= w[1].depth));

    let (_, limited) = traverse_symbol(
        &store,
        &active,
        "python.pkg.service.Kennel",
        Direction::Outgoing,
        &[],
        3,
        2,
    )
    .unwrap();
    assert_eq!(limited.len(), 2);
}
