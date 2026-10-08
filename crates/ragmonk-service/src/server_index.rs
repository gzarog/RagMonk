//! Server-mode source passes (ADR 0033, P0-S02).
//!
//! The knowledge pipeline itself is backend-agnostic: scan, metadata-first
//! diff, conversion, parsing, chunking, cross-file resolution, linking and
//! embedding run exactly as in local mode, into a **disposable staging
//! store** under `<home>/cache/server-staging/<index_prefix>/`. The
//! server stays authoritative:
//!
//! * The pass holds the source's server writer lease (fencing token) for
//!   its whole duration, renewed in the background; a superseded or
//!   expired holder cannot publish, abort or delete newer state.
//! * Before the pass, the staging store must match the server's active
//!   build (recorded in `synced/<source_id>`). If it does not (another host
//!   published, the cache was deleted, or the server was reset), the
//!   staging copy of that source is discarded and rebuilt from the
//!   original files; copy-forward on the server still skips every file
//!   whose derived knowledge did not change.
//! * The staged build is published with [`ServerBackend::publish_from_store`]:
//!   unchanged files are copied forward server-side, changed files are
//!   bulk-written, the swap is an atomic compare-and-swap, and any error
//!   aborts the pending build while the previous one stays searchable.
//! * An offline source is recorded as offline on the server and nothing is
//!   published, so its previously published knowledge is never deleted.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use ragmonk_backends::backend::Lease;
use ragmonk_backends::publish::PublishInput;
use ragmonk_backends::ServerBackend;
use ragmonk_config::RagMonkConfig;
use ragmonk_core::errors::RagMonkError;
use ragmonk_core::ids::record;
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::coordinator::{
    run_source_with, Options, Progress, ProgressEvent, Registry, SourceResult,
};
use ragmonk_storage::control::{ControlPlane, NewSource, SourceRecord};
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::StorageLayout;

use crate::backend::server_err;
use crate::indexing::{lease_owner, RunOptions};
use crate::{db, generic};

/// `<home>/cache/server-staging/<index_prefix>`.
pub fn staging_root(home: &Home, cfg: &RagMonkConfig) -> PathBuf {
    let prefix: String = cfg
        .storage
        .server
        .index_prefix
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect();
    home.cache_dir().join("server-staging").join(prefix)
}

fn marker(root: &std::path::Path, source_id: &str) -> PathBuf {
    root.join("synced").join(source_id)
}

/// Deletes a source's staging data (never its original files).
pub fn drop_staging(home: &Home, cfg: &RagMonkConfig, source_id: &str) {
    let root = staging_root(home, cfg);
    let layout = StorageLayout::at(&root);
    if let Ok(mut cp) = open_staging(&layout, cfg) {
        if let Ok(Some(s)) = cp.get_source(source_id) {
            let _ = std::fs::remove_dir_all(layout.project_dir(&project_id_for_canonical(&s.path)));
            let _ = cp.remove_source(source_id);
        }
    }
    let _ = std::fs::remove_file(marker(&root, source_id));
}

/// Opens the staging control plane. Parallel workers (and a concurrent
/// process) may race to create it: creation is serialized in-process and a
/// lost cross-process race is retried once the winner finished.
fn open_staging(layout: &StorageLayout, cfg: &RagMonkConfig) -> Result<ControlPlane, RagMonkError> {
    static CREATE: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _g = CREATE.lock().unwrap_or_else(|p| p.into_inner());
    let mut last = None;
    for _ in 0..5 {
        match ControlPlane::open(layout, cfg.runtime.sqlite_cache_size_mb) {
            Ok(cp) => return Ok(cp),
            Err(e) => {
                last = Some(e);
                std::thread::sleep(Duration::from_millis(100));
            }
        }
    }
    Err(db(last.map_or_else(
        || "cannot open staging".to_owned(),
        |e| e.to_string(),
    )))
}

/// Keeps a lease alive until dropped.
struct Renewal<'a> {
    stop: &'a AtomicBool,
}

impl Drop for Renewal<'_> {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
    }
}

/// One server-mode source pass. The caller holds the source's local lock.
#[allow(clippy::too_many_arguments)]
pub fn run_source(
    home: &Home,
    cfg: &RagMonkConfig,
    server: &ServerBackend,
    source: &SourceRecord,
    registry: &Registry,
    opts: &Options,
    run: &RunOptions,
    progress: &mut dyn Progress,
) -> Result<SourceResult, RagMonkError> {
    let ttl = Duration::from_secs_f64(cfg.storage.server.lease_seconds);
    let lease = server
        .acquire_lease(&source.id, &lease_owner(), ttl)
        .map_err(server_err)?;
    let stop = AtomicBool::new(false);
    let lost = AtomicBool::new(false);
    let result = std::thread::scope(|s| {
        let (stop, lost, lease_ref) = (&stop, &lost, &lease);
        s.spawn(move || {
            // Renew at a third of the TTL; a failed renewal means the lease
            // was superseded: the publish will be refused by the server.
            let step = (ttl / 3).max(Duration::from_millis(500));
            let mut waited = Duration::ZERO;
            while !stop.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(100));
                waited += Duration::from_millis(100);
                if waited >= step {
                    waited = Duration::ZERO;
                    if server.renew_lease(lease_ref).is_err() {
                        lost.store(true, Ordering::SeqCst);
                        return;
                    }
                }
            }
        });
        let _renewal = Renewal { stop };
        staged_pass(
            home, cfg, server, source, registry, opts, run, &lease, progress,
        )
    });
    if lost.load(Ordering::SeqCst) {
        tracing::warn!(component = "indexing", event = "lease_lost", source_id = %source.id);
    }
    let _ = server.release_lease(&lease);
    result
}

#[allow(clippy::too_many_arguments)]
fn staged_pass(
    home: &Home,
    cfg: &RagMonkConfig,
    server: &ServerBackend,
    source: &SourceRecord,
    registry: &Registry,
    opts: &Options,
    run: &RunOptions,
    lease: &Lease,
    progress: &mut dyn Progress,
) -> Result<SourceResult, RagMonkError> {
    let root = staging_root(home, cfg);
    std::fs::create_dir_all(root.join("synced")).map_err(generic)?;
    let layout = StorageLayout::at(&root);
    let mut cp = open_staging(&layout, cfg)?;
    let staged = match cp.get_source(&source.id).map_err(db)? {
        Some(s) => s,
        None => {
            let (s, _) = cp
                .add_source(&NewSource {
                    canonical_path: source.path.clone(),
                    source_type: source.source_type,
                    enabled: true,
                    include_patterns: source.include_patterns.clone(),
                    exclude_patterns: source.exclude_patterns.clone(),
                })
                .map_err(db)?;
            s
        }
    };
    if staged.id != source.id {
        return Err(RagMonkError::config(format!(
            "staging id {} does not match server source {}; delete {} and retry",
            staged.id,
            source.id,
            root.display()
        )));
    }
    let server_state = server.source_state(&source.id).map_err(server_err)?;
    let server_active = server_state
        .as_ref()
        .and_then(|s| s.active_build_id.clone());
    let synced = std::fs::read_to_string(marker(&root, &source.id))
        .ok()
        .map(|s| s.trim().to_owned());
    if synced != server_active || run.force_full {
        // The staging copy does not describe the server's active build:
        // discard it and rebuild from the original files.
        let project_dir = layout.project_dir(&project_id_for_canonical(&source.path));
        let _ = std::fs::remove_dir_all(&project_dir);
        cp.reset_index_state(
            &source.id,
            if run.force_full {
                "manual rebuild"
            } else {
                "staging cache does not match the server's active build"
            },
        )
        .map_err(db)?;
        let _ = std::fs::remove_file(marker(&root, &source.id));
    }
    let mut result = run_source_with(
        &layout,
        &mut cp,
        source,
        registry,
        opts,
        run.targets.as_ref(),
        progress,
    )?;
    if let Some(reason) = &result.offline {
        // Offline: record it and keep the published knowledge untouched.
        server
            .set_online(&source.id, Some(lease), false, Some(reason))
            .map_err(server_err)?;
        return Ok(result);
    }
    let state = cp.state(&source.id).map_err(db)?;
    let Some(local_build) = state.active_build_id.clone() else {
        return Err(RagMonkError::new(
            ragmonk_core::errors::ErrorKind::Generic,
            "staging pass produced no build to publish",
        ));
    };
    let store = ProjectStore::open(
        &layout,
        &project_id_for_canonical(&source.path),
        &source.id,
        cfg.runtime.sqlite_cache_size_mb,
    )
    .map_err(db)?;
    progress.event(&ProgressEvent::Stage {
        source_id: source.id.clone(),
        stage: "publish",
    });
    let build_id = record::build_id(
        &source.id,
        &format!(
            "{}-{}-{}",
            ragmonk_indexing::progress::now_iso(),
            std::process::id(),
            lease.token
        ),
    );
    let report = server
        .publish_from_store(&PublishInput {
            source_id: &source.id,
            path: &source.path,
            store: &store,
            local_build: &local_build,
            versions: serde_json::to_value(&registry.versions).map_err(generic)?,
            full: run.force_full,
            lease: Some(lease),
            build_id: &build_id,
        })
        .map_err(server_err)?;
    server
        .set_online(&source.id, Some(lease), true, None)
        .map_err(server_err)?;
    if let Some(b) = &report.build_id {
        std::fs::write(marker(&root, &source.id), b).map_err(generic)?;
        result.build_id = Some(b.clone());
    }
    result.published = report.published;
    tracing::info!(
        component = "indexing",
        event = "server_publish",
        source_id = %source.id,
        published = report.published,
        files = report.files,
        files_copied = report.files_copied,
        files_written = report.files_written,
        records_copied = report.records_copied,
        records_written = report.records_written,
        relationships_written = report.relationships_written,
        seconds = report.seconds,
    );
    result.server_publish = Some(serde_json::to_value(&report).map_err(generic)?);
    Ok(result)
}
