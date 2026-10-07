//! ANN index maintenance: rebuild from stored vectors, and backfill
//! vectors that were never computed.

use ragmonk_core::errors::RagMonkError;
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_service::load;
use ragmonk_service::query::open_sources;
use ragmonk_storage::StorageLayout;
use serde_json::{json, Value};

use crate::{dberr, generic, index_lock};

/// Rebuilds each selected source's ANN index from its stored vectors.
/// One `{source_id, backend, vectors}` per source; `backend` is null when
/// the source has no vectors yet.
pub fn rebuild(home: &Home, source: Option<&str>) -> Result<Vec<Value>, RagMonkError> {
    let layout = StorageLayout::new(home);
    let spec = ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL;
    let fp = spec.fingerprint();
    let lock = index_lock(home, "vectors-rebuild")?;
    let mut rebuilt = Vec::new();
    for o in open_sources(home, source)? {
        let n = o.store.embedding_keys(&o.build, &fp).map_err(dberr)?.len();
        if n == 0 {
            rebuilt.push(json!({"source_id": o.source.id, "backend": null, "vectors": 0}));
            continue;
        }
        let dir = layout.project_dir(&project_id_for_canonical(&o.source.path));
        let _ = std::fs::remove_file(ragmonk_ml::ann::index_path(&dir));
        let stats =
            ragmonk_ml::ann::sync(&o.store, &dir, &o.build, &fp, spec.dims).map_err(generic)?;
        rebuilt.push(json!({"source_id": o.source.id, "backend": "hnsw", "vectors": stats.live}));
    }
    lock.release();
    Ok(rebuilt)
}

/// Computes missing vectors for each selected source and syncs its ANN
/// index. One `{source_id, embedded}` per source.
pub fn backfill(home: &Home, source: Option<&str>) -> Result<Vec<Value>, RagMonkError> {
    let layout = StorageLayout::new(home);
    let spec = ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL;
    let cfg = load(home)?;
    let lazy = ragmonk_ml::LazyEmbedder::new(
        ragmonk_ml::embedder::models_root(Some(&home.models_dir())),
        spec,
        cfg.indexing.embedding_batch_size,
    );
    let embedder = lazy.get().map_err(|e| {
        RagMonkError::usage(format!(
            "the embedding model is not available ({e}); install it first"
        ))
    })?;
    let lock = index_lock(home, "vectors-backfill")?;
    let mut done = Vec::new();
    for mut o in open_sources(home, source)? {
        let stats = ragmonk_ml::embed_build(
            &mut o.store,
            &o.build,
            embedder,
            ragmonk_documents::chunker::EMBEDDING_TEXT_VERSION,
        )
        .map_err(|e| generic(format!("{}: {}", e.code, e.message)))?;
        let embedded = stats.inferred + stats.cache_reused;
        if embedded > 0 {
            let dir = layout.project_dir(&project_id_for_canonical(&o.source.path));
            ragmonk_ml::ann::sync(
                &o.store,
                &dir,
                &o.build,
                embedder.fingerprint(),
                embedder.spec().dims,
            )
            .map_err(generic)?;
        }
        done.push(json!({"source_id": o.source.id, "embedded": embedded}));
    }
    lock.release();
    Ok(done)
}
