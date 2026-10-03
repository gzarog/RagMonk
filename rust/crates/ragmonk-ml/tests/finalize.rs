//! Embedding finalizer: completeness, cache reuse, explicit rebuild on a
//! model change, crash recovery and the model-unavailable path.

mod common;

use std::path::{Path, PathBuf};

use ragmonk_core::paths::project_id_for_canonical;
use ragmonk_indexing::coordinator::{run_source, NoProgress, Options};
use ragmonk_ml::embedder::Embedder;
use ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL;
use ragmonk_storage::control::{ControlPlane, SourceRecord};
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::V2Layout;

fn compat() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../compat")
}

fn copy_tree(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).unwrap();
    for e in std::fs::read_dir(from).unwrap() {
        let p = e.unwrap().path();
        let dest = to.join(p.file_name().unwrap());
        if p.is_dir() {
            copy_tree(&p, &dest);
        } else {
            std::fs::copy(&p, dest).unwrap();
        }
    }
}

/// `RAGMONK_MODELS_DIR` when it holds the default model (see parity.rs).
fn models_root() -> Option<PathBuf> {
    let root = std::env::var_os("RAGMONK_MODELS_DIR").map(PathBuf::from);
    match root {
        Some(r)
            if r.join(DEFAULT_EMBEDDING_MODEL.slug)
                .join("model.safetensors")
                .exists() =>
        {
            Some(r)
        }
        _ if std::env::var("RAGMONK_REQUIRE_MODELS").as_deref() == Ok("1") => {
            panic!("RAGMONK_REQUIRE_MODELS=1 but the embedding model is not installed")
        }
        _ => {
            eprintln!("skipping: embedding model not installed (set RAGMONK_MODELS_DIR)");
            None
        }
    }
}

struct Fx {
    _tmp: tempfile::TempDir,
    root: PathBuf,
    layout: V2Layout,
    cp: ControlPlane,
    src: SourceRecord,
}

fn fixture() -> Fx {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path().join("corpus");
    copy_tree(&compat().join("fixtures/linking"), &root);
    let home = common::home(tmp.path());
    let layout = V2Layout::new(&home);
    let mut cp = common::control(&home);
    let src = common::add_source(&mut cp, &root, &[], &[]);
    Fx {
        _tmp: tmp,
        root,
        layout,
        cp,
        src,
    }
}

impl Fx {
    fn index(&mut self, models_root: &Path) {
        let mut cfg = ragmonk_config::RagMonkConfig::default();
        cfg.search.semantic = true;
        let opts = ragmonk_convert::RegistryOptions {
            models_root: Some(models_root.to_path_buf()),
            ..Default::default()
        };
        let r = run_source(
            &self.layout,
            &mut self.cp,
            &self.src,
            &ragmonk_convert::registry_with(&cfg, &opts),
            &Options::from_config(&cfg),
            &mut NoProgress,
        )
        .unwrap();
        assert!(r.published || r.build_id.is_some(), "{r:?}");
    }

    fn store(&self) -> (ProjectStore, String) {
        let active = self
            .cp
            .state(&self.src.id)
            .unwrap()
            .active_build_id
            .unwrap();
        let (s, _) = ProjectStore::open(
            &self.layout,
            &project_id_for_canonical(&self.src.path),
            &self.src.id,
            8,
        )
        .unwrap();
        (s, active)
    }
}

fn subjects(s: &ProjectStore, b: &str) -> i64 {
    s.count("entities", b).unwrap() + s.count("chunks", b).unwrap()
}

#[test]
fn missing_model_leaves_vectors_pending_until_it_is_installed() {
    let mut fx = fixture();
    let empty = tempfile::tempdir().unwrap();
    // RAGMONK_MODELS_DIR would override the explicit root; this test only
    // runs the unavailable path when it is unset.
    if std::env::var_os("RAGMONK_MODELS_DIR").is_some() {
        return;
    }
    fx.index(empty.path());
    let (s, b) = fx.store();
    assert!(subjects(&s, &b) > 0);
    assert!(s.embeddings(&b).unwrap().is_empty());
}

#[test]
fn every_subject_gets_a_current_vector_and_reruns_reuse_the_cache() {
    let Some(models) = models_root() else { return };
    let mut fx = fixture();
    fx.index(&models);
    let (s, b) = fx.store();
    let fp = DEFAULT_EMBEDDING_MODEL.fingerprint();
    let stored = s.embeddings(&b).unwrap();
    let pending = s.pending_embedding_subjects(&b, &fp).unwrap();
    assert!(pending.is_empty(), "{pending:?}");
    assert!(!stored.is_empty());
    assert!(stored
        .iter()
        .all(|e| e.model_fingerprint == fp && e.vector.len() == DEFAULT_EMBEDDING_MODEL.dims));
    let cache = s.embedding_cache_len().unwrap();
    assert!(cache > 0 && cache <= stored.len() as i64);
    drop(s);

    // Touch one document: only its chunks are re-embedded, and unchanged
    // texts come from the cache (no new cache rows for them).
    let doc = fx.root.join("docs/billing.md");
    let mut text = std::fs::read_to_string(&doc).unwrap();
    text.push_str("\n\n## Appendix\n\nA brand new paragraph about ledgers.\n");
    std::fs::write(&doc, text).unwrap();
    common::bump_mtime(&doc, 5);
    fx.index(&models);
    let (s, b2) = fx.store();
    assert!(s.pending_embedding_subjects(&b2, &fp).unwrap().is_empty());
    let after = s.embeddings(&b2).unwrap();
    let new_cache = s.embedding_cache_len().unwrap();
    assert!(new_cache > cache, "the new paragraph is a new text");
    assert!(new_cache - cache <= 2, "unchanged chunks reuse the cache");
    // Vectors of untouched subjects are identical across builds.
    let touched = s
        .files(&b2)
        .unwrap()
        .into_iter()
        .find(|f| f.rel_path == "docs/billing.md")
        .map(|f| f.id)
        .unwrap();
    for e in stored.iter().filter(|e| e.file_id != touched) {
        let a = after.iter().find(|a| a.subject_id == e.subject_id).unwrap();
        assert_eq!(a.vector, e.vector);
    }
}

#[test]
fn a_model_change_rebuilds_every_vector_explicitly() {
    let Some(models) = models_root() else { return };
    let mut fx = fixture();
    fx.index(&models);
    let (mut s, b) = fx.store();
    let total = s.embeddings(&b).unwrap().len();
    // Same weights, different preprocessing contract => new fingerprint.
    let mut spec = DEFAULT_EMBEDDING_MODEL;
    spec.max_chars = 2000;
    let changed = Embedder::load(&models.join(spec.slug), spec).unwrap();
    let stats = ragmonk_ml::embed_build(&mut s, &b, &changed, "2").unwrap();
    assert_eq!(stats.stale_dropped, total);
    assert_eq!(stats.subjects, total);
    let fp = spec.fingerprint();
    let after = s.embeddings(&b).unwrap();
    assert_eq!(after.len(), total);
    assert!(after.iter().all(|e| e.model_fingerprint == fp));
}

#[test]
fn vectors_lost_in_a_crash_are_recomputed_on_the_next_run() {
    let Some(models) = models_root() else { return };
    let mut fx = fixture();
    fx.index(&models);
    let (s, b) = fx.store();
    let total = s.embeddings(&b).unwrap().len();
    s.connection()
        .execute(
            "DELETE FROM embeddings WHERE rowid IN
                (SELECT rowid FROM embeddings WHERE build_id = ?1 LIMIT 5)",
            [&b],
        )
        .unwrap();
    assert_eq!(s.embeddings(&b).unwrap().len(), total - 5);
    drop(s);
    fx.index(&models);
    let (s, b) = fx.store();
    assert_eq!(s.embeddings(&b).unwrap().len(), total);
}
