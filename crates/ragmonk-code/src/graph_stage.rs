//! The post-index relationship graph stage.
//!
//! Runs only after a source's base index is published, against that exact
//! published build, and never changes a base row:
//!
//! 1. The recorded [`GraphState`] decides the work: nothing when the graph
//!    is `ready` for the active base generation and derivation; a full
//!    recomputation when the graph belongs to another base build, another
//!    derivation, or has no dependency metadata; otherwise only the files
//!    whose dependency key (content hash + base write time) changed.
//! 2. Each code file to derive is re-read from the source root and its
//!    sha256 compared with the published `content_hash`. A mismatch means
//!    the file changed after indexing: the graph is marked `stale` and
//!    nothing is published (never a graph of mixed snapshots); the next
//!    index pass picks the change up and the graph is retried against it.
//! 3. In one SQLite transaction: verify the base generation is still the
//!    one planned against, replace the changed files' relationships,
//!    re-resolve cross-file references, relink, re-apply manual link intent
//!    and promote the new graph generation. Re-resolution is scoped by the
//!    per-file symbol dependencies ([`FileSymbols`](ragmonk_storage::graph::FileSymbols)): the changed files,
//!    plus every unchanged file with a reference whose qualified or bare
//!    name matches a definition the changed or removed files had before or
//!    have now (added, removed, moved or newly ambiguous targets).
//!    Candidates are looked up per symbol, never by loading the build, and
//!    only rows whose outcome changed are rewritten. Any error
//!    rolls everything back and records `failed`; the base index stays
//!    published and searchable throughout.
//!
//! Work and memory are bounded: files are parsed `workers` at a time in
//! batches of [`GRAPH_BATCH`], each batch written before the next is read.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use ragmonk_storage::graph::{GraphFile, GraphLifecycle, GraphState};
use ragmonk_storage::knowledge::{ProjectStore, RelationshipRow};
use ragmonk_storage::StorageError;
use serde::Serialize;

use crate::process::{file_relationships, graph_derivation, resolve_build, resolve_files};

/// Most files parsed before their relationships are written.
pub const GRAPH_BATCH: usize = 256;

/// Builds code-document links for the graph stage (implemented by the
/// knowledge linker; `None` derives code relationships only).
pub trait GraphLinker: Sync {
    /// Links `touched` files (every file when `full`) and re-applies
    /// manual link intent. Runs inside the graph transaction.
    fn link(
        &self,
        store: &mut ProjectStore,
        build_id: &str,
        touched: &[String],
        full: bool,
    ) -> Result<usize, StorageError>;
}

pub struct GraphOptions<'a> {
    /// Parser threads (at least 1).
    pub workers: usize,
    pub linker: Option<&'a dyn GraphLinker>,
    /// Checked between batches; a cancelled build publishes nothing.
    pub cancel: Option<&'a AtomicBool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GraphOutcome {
    /// The published graph already matches the active base.
    UpToDate,
    Built,
}

/// What one graph build did.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct GraphReport {
    pub outcome: GraphOutcome,
    /// Recomputed from scratch (no usable dependency metadata).
    pub full: bool,
    pub generation: u64,
    pub base_generation: String,
    /// Files whose graph rows were recomputed.
    pub files_processed: usize,
    pub relationships_written: usize,
    /// Cross-file references whose resolution changed.
    pub resolutions_changed: usize,
    /// Files whose cross-file references were re-resolved (every file for
    /// a full build; changed files plus their symbol dependents otherwise).
    pub files_reresolved: usize,
    pub links: usize,
    pub seconds: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum GraphError {
    /// No published base build to derive from.
    #[error("no published index to build relationships from")]
    NotPublished,
    /// The inputs no longer match the published base (a file changed on
    /// disk, or the base was republished); retried after the next index.
    #[error("relationships are stale: {0}")]
    Stale(String),
    #[error("relationship build cancelled")]
    Cancelled,
    #[error("relationship build failed: {0}")]
    Failed(String),
}

fn failed(e: impl std::fmt::Display) -> GraphError {
    GraphError::Failed(e.to_string())
}

/// Progress of one graph build: `(files done, files to process)`.
pub type GraphProgressFn<'a> = dyn FnMut(usize, usize) + 'a;

/// Builds (or confirms) the relationship graph of `build_id`, the source's
/// active published build. The caller holds the source's lock (and lease).
pub fn build_graph(
    store: &mut ProjectStore,
    root: &Path,
    build_id: &str,
    opts: &GraphOptions<'_>,
    progress: &mut GraphProgressFn<'_>,
) -> Result<GraphReport, GraphError> {
    let started = Instant::now();
    let Some(generation) = store.base_generation(build_id).map_err(failed)? else {
        return Err(GraphError::NotPublished);
    };
    let derivation = graph_derivation();
    let prev = store.graph_state_with_files().map_err(failed)?;
    if prev.state == GraphLifecycle::Ready
        && prev.base_generation.as_deref() == Some(generation.as_str())
        && prev.derivation_version.as_deref() == Some(derivation.as_str())
    {
        return Ok(GraphReport {
            outcome: GraphOutcome::UpToDate,
            full: false,
            generation: prev.generation,
            base_generation: generation,
            files_processed: 0,
            relationships_written: 0,
            resolutions_changed: 0,
            files_reresolved: 0,
            links: 0,
            seconds: started.elapsed().as_secs_f64(),
        });
    }
    store
        .set_graph_lifecycle(GraphLifecycle::Building, prev.last_error.as_deref(), None)
        .map_err(failed)?;
    let outcome = build(
        store,
        root,
        build_id,
        &generation,
        &derivation,
        &prev,
        opts,
        progress,
    );
    match outcome {
        Ok(mut report) => {
            report.seconds = started.elapsed().as_secs_f64();
            Ok(report)
        }
        Err(e) => {
            if store.in_session() {
                let _ = store.rollback_session();
            }
            let (state, error, stale) = match &e {
                GraphError::Stale(reason) => (GraphLifecycle::Stale, None, Some(reason.as_str())),
                GraphError::Cancelled => (
                    GraphLifecycle::Failed,
                    Some("cancelled before completion"),
                    None,
                ),
                GraphError::Failed(m) => (GraphLifecycle::Failed, Some(m.as_str()), None),
                GraphError::NotPublished => (GraphLifecycle::Pending, None, None),
            };
            let _ = store.set_graph_lifecycle(state, error, stale);
            Err(e)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn build(
    store: &mut ProjectStore,
    root: &Path,
    build_id: &str,
    generation: &str,
    derivation: &str,
    prev: &GraphState,
    opts: &GraphOptions<'_>,
    progress: &mut GraphProgressFn<'_>,
) -> Result<GraphReport, GraphError> {
    let files = store.graph_files(build_id).map_err(failed)?;
    // Incremental only on top of a complete record of this base build:
    // its file keys and its per-file symbol dependencies.
    let full = prev.base_build_id.as_deref() != Some(build_id)
        || prev.derivation_version.as_deref() != Some(derivation)
        || prev.files.is_empty()
        || prev.symbols.is_empty();
    let changed: Vec<&GraphFile> = files
        .iter()
        .filter(|f| full || prev.files.get(&f.id) != Some(&f.key))
        .collect();
    let current: std::collections::BTreeSet<&str> = files.iter().map(|f| f.id.as_str()).collect();
    let removed: Vec<&String> = prev
        .files
        .keys()
        .filter(|f| !current.contains(f.as_str()))
        .collect();
    let total = changed.len();
    progress(0, total);

    store.begin_session().map_err(failed)?;
    // Fenced against a concurrent publication: the base generation must
    // still be the one this build was planned against.
    if store.base_generation(build_id).map_err(failed)?.as_deref() != Some(generation) {
        return Err(GraphError::Stale(
            "the base index was republished during the relationship build".into(),
        ));
    }
    if full {
        store.clear_graph(build_id).map_err(failed)?;
    } else {
        for f in &changed {
            store.clear_file_graph(build_id, &f.id).map_err(failed)?;
        }
    }
    let workers = opts.workers.max(1);
    let mut done = 0;
    let mut written = 0;
    for batch in changed.chunks(GRAPH_BATCH) {
        if opts.cancel.is_some_and(|c| c.load(Ordering::Relaxed)) {
            return Err(GraphError::Cancelled);
        }
        ragmonk_indexing::progress::heartbeat();
        let code: Vec<&GraphFile> = batch
            .iter()
            .copied()
            .filter(|f| f.kind == "code" && f.status == "indexed")
            .collect();
        for rows in derive_batch(root, &code, workers)? {
            written += rows.len();
            store.put_relationships(build_id, &rows).map_err(failed)?;
        }
        done += batch.len();
        progress(done, total);
    }
    let changed_ids: Vec<String> = changed.iter().map(|f| f.id.clone()).collect();
    let (symbols, resolutions_changed, files_reresolved) = if full {
        let n = resolve_build(store, build_id).map_err(failed)?;
        let symbols = store.file_symbols(build_id, None).map_err(failed)?;
        (symbols, n, files.len())
    } else {
        // Symbols whose definitions changed: the old definitions of every
        // changed or removed file and the new ones of every changed file.
        let fresh = store
            .file_symbols(build_id, Some(&changed_ids))
            .map_err(failed)?;
        let mut keys: std::collections::BTreeSet<&str> = std::collections::BTreeSet::new();
        for f in changed_ids.iter().chain(removed.iter().copied()) {
            if let Some(s) = prev.symbols.get(f) {
                keys.extend(s.defs.iter().map(String::as_str));
            }
            if let Some(s) = fresh.get(f) {
                keys.extend(s.defs.iter().map(String::as_str));
            }
        }
        // Re-resolve the changed files and every unchanged file with a
        // reference through one of those symbols.
        let mut targets: std::collections::BTreeSet<String> = changed_ids.iter().cloned().collect();
        for (f, s) in &prev.symbols {
            if current.contains(f.as_str()) && s.refs.iter().any(|k| keys.contains(k.as_str())) {
                targets.insert(f.clone());
            }
        }
        let n = resolve_files(store, build_id, &targets).map_err(failed)?;
        let mut symbols = prev.symbols.clone();
        for f in &removed {
            symbols.remove(*f);
        }
        symbols.extend(fresh);
        (symbols, n, targets.len())
    };
    let links = match opts.linker {
        Some(l) => l
            .link(store, build_id, &changed_ids, full)
            .map_err(failed)?,
        None => 0,
    };
    let state = GraphState {
        state: GraphLifecycle::Ready,
        generation: prev.generation + 1,
        base_build_id: Some(build_id.to_owned()),
        base_generation: Some(generation.to_owned()),
        derivation_version: Some(derivation.to_owned()),
        files: files
            .iter()
            .map(|f| (f.id.clone(), f.key.clone()))
            .collect(),
        symbols,
        last_success_at: Some(ragmonk_storage::db::now_iso()),
        last_error: None,
        stale_reason: None,
        updated_at: None,
    };
    store.put_graph_state_with_files(&state).map_err(failed)?;
    store.commit_session().map_err(failed)?;
    Ok(GraphReport {
        outcome: GraphOutcome::Built,
        full,
        generation: state.generation,
        base_generation: generation.to_owned(),
        files_processed: total,
        relationships_written: written,
        resolutions_changed,
        files_reresolved,
        links,
        seconds: 0.0,
    })
}

/// Reads, verifies and parses `files` on up to `workers` threads, in input
/// order.
fn derive_batch(
    root: &Path,
    files: &[&GraphFile],
    workers: usize,
) -> Result<Vec<Vec<RelationshipRow>>, GraphError> {
    let per = files.len().div_ceil(workers.max(1)).max(1);
    let parts: Vec<Result<Vec<Vec<RelationshipRow>>, GraphError>> = std::thread::scope(|s| {
        let handles: Vec<_> = files
            .chunks(per)
            .map(|part| s.spawn(move || part.iter().map(|f| derive_file(root, f)).collect()))
            .collect();
        handles
            .into_iter()
            .map(|h| {
                h.join()
                    .unwrap_or_else(|_| Err(failed("relationship derivation panicked")))
            })
            .collect()
    });
    let mut out = Vec::with_capacity(files.len());
    for p in parts {
        out.extend(p?);
    }
    Ok(out)
}

fn derive_file(root: &Path, f: &GraphFile) -> Result<Vec<RelationshipRow>, GraphError> {
    let source = std::fs::read(root.join(&f.rel_path)).map_err(|e| {
        GraphError::Stale(format!(
            "{} cannot be read since it was indexed ({e})",
            f.rel_path
        ))
    })?;
    let hash = ragmonk_indexing::fingerprint::hash_bytes(&source);
    if f.content_hash.as_deref() != Some(hash.as_str()) {
        return Err(GraphError::Stale(format!(
            "{} changed since it was indexed",
            f.rel_path
        )));
    }
    // The base index parsed these exact bytes; a parse failure here would
    // be a derivation bug, recorded as a failure rather than hidden.
    file_relationships(&f.rel_path, &f.id, &source).map_err(|p| {
        failed(format!(
            "{}: syntax error(s) in a {} file",
            f.rel_path, p.language
        ))
    })
}

/// Marks the graph disabled (`indexing.relationships_enabled: false`):
/// retained graph rows are hidden, nothing else is touched.
pub fn mark_disabled(store: &mut ProjectStore) -> Result<(), StorageError> {
    let s = store.graph_state()?;
    if s.state != GraphLifecycle::Disabled {
        store.set_graph_lifecycle(GraphLifecycle::Disabled, None, None)?;
    }
    Ok(())
}
