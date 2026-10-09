//! Server-mode source passes (ADR 0033, P0-S02).
//!
//! The knowledge pipeline itself is backend-agnostic: scan, metadata-first
//! diff, conversion, parsing, chunking and embedding run exactly as in
//! local mode, into a **disposable staging store** under
//! `<home>/cache/server-staging/<index_prefix>/`. The relationship graph is
//! derived afterwards, in the staging store, by [`build_relationships`] and
//! published as an independent graph generation. The server stays
//! authoritative:
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
use ragmonk_backends::status::RuntimeDoc;
use ragmonk_backends::ServerBackend;
use ragmonk_config::RagMonkConfig;
use ragmonk_core::errors::RagMonkError;
use ragmonk_core::ids::record;
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::coordinator::{
    run_source_with, Options, Progress, ProgressEvent, Registry, SourceResult,
};
use ragmonk_indexing::progress::{now_iso, SourceProgress, HEARTBEAT_INTERVAL};
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
    let beat = RuntimeHeartbeat::new(server, &lease, source, run, ttl);
    beat.write();
    let stop = AtomicBool::new(false);
    let lost = AtomicBool::new(false);
    let result = std::thread::scope(|s| {
        let (stop, lost, lease_ref, beat_ref) = (&stop, &lost, &lease, &beat);
        s.spawn(move || {
            // Renew at a third of the TTL; a failed renewal means the lease
            // was superseded: the publish will be refused by the server.
            // The run heartbeat is independent of file progress, so long
            // stages stay visibly alive to every host.
            let step = (ttl / 3).max(Duration::from_millis(500));
            let mut waited = Duration::ZERO;
            let mut since_beat = Duration::ZERO;
            while !stop.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(100));
                waited += Duration::from_millis(100);
                since_beat += Duration::from_millis(100);
                if since_beat >= HEARTBEAT_INTERVAL {
                    since_beat = Duration::ZERO;
                    beat_ref.beat();
                }
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
        let mut reporting = Reporting {
            inner: progress,
            beat: &beat,
        };
        staged_pass(
            home,
            cfg,
            server,
            source,
            registry,
            opts,
            run,
            &lease,
            &mut reporting,
        )
    });
    if lost.load(Ordering::SeqCst) {
        tracing::warn!(component = "indexing", event = "lease_lost", source_id = %source.id);
    }
    beat.finish(result.as_ref().err().map(RagMonkError::message));
    let _ = server.release_lease(&lease);
    result
}

/// The pass's `{prefix}-runtime` heartbeat document: written on every
/// stage change, at most every [`HEARTBEAT_INTERVAL`] for counters, by the
/// independent heartbeat, and once with the terminal outcome. Fenced by
/// the lease token; a failed write never fails the pass.
struct RuntimeHeartbeat<'a> {
    server: &'a ServerBackend,
    base: RuntimeDoc,
    ttl: Duration,
    state: std::sync::Mutex<(SourceProgress, Option<std::time::Instant>)>,
}

impl<'a> RuntimeHeartbeat<'a> {
    fn new(
        server: &'a ServerBackend,
        lease: &Lease,
        source: &SourceRecord,
        run: &RunOptions,
        ttl: Duration,
    ) -> Self {
        let now = now_iso();
        Self {
            server,
            base: RuntimeDoc {
                source_id: source.id.clone(),
                run_id: run
                    .run_id
                    .clone()
                    .unwrap_or_else(crate::indexing::new_run_id),
                host: ragmonk_indexing::runtime::host_name(),
                owner: lease.owner.clone(),
                lease_token: lease.token,
                operation: Some(if run.force_full { "rebuild" } else { "index" }.into()),
                ..RuntimeDoc::default()
            },
            ttl,
            state: std::sync::Mutex::new((SourceProgress::new(&source.id, &now), None)),
        }
    }

    fn with<R>(&self, f: impl FnOnce(&mut (SourceProgress, Option<std::time::Instant>)) -> R) -> R {
        let mut g = self.state.lock().unwrap_or_else(|p| p.into_inner());
        f(&mut g)
    }

    fn apply(&self, event: &ProgressEvent) {
        if ragmonk_indexing::progress::event_source(event)
            .is_some_and(|id| id != self.base.source_id)
        {
            return;
        }
        let due = self.with(|(p, last)| {
            let structural = p.apply(event, &now_iso());
            structural || last.is_none_or(|t| t.elapsed() >= HEARTBEAT_INTERVAL)
        });
        if due {
            self.write();
        }
    }

    fn beat(&self) {
        self.with(|(p, _)| p.heartbeat_at = Some(now_iso()));
        self.write();
    }

    fn finish(&self, error: Option<&str>) {
        self.with(|(p, _)| p.finish(error.is_none(), &now_iso()));
        self.write_doc(error);
    }

    fn write(&self) {
        self.write_doc(None);
    }

    fn write_doc(&self, error: Option<&str>) {
        let ms = |v: &Option<String>| {
            v.as_deref()
                .and_then(ragmonk_backends::backend::iso_to_millis)
        };
        let doc = self.with(|(p, last)| {
            *last = Some(std::time::Instant::now());
            let now = chrono::Utc::now();
            let expires = if p.active {
                now + chrono::Duration::from_std(self.ttl.max(HEARTBEAT_INTERVAL * 3))
                    .unwrap_or_else(|_| chrono::Duration::seconds(60))
            } else {
                now
            };
            RuntimeDoc {
                stage: p.stage.clone(),
                active: p.active,
                outcome: p.outcome.clone(),
                scanned: p.scanned,
                planned: p.planned,
                processed: p.processed,
                indexed: p.indexed,
                failed: p.failed,
                retry: p.retry,
                error: error.map(|e| {
                    ragmonk_telemetry::redact::redact_urls_in_text(e)
                        .chars()
                        .take(500)
                        .collect()
                }),
                started_at: ms(&p.started_at),
                last_progress_at: ms(&p.last_progress_at),
                heartbeat_at: ms(&p.heartbeat_at),
                expires_at: Some(expires.timestamp_millis().to_string()),
                finished_at: ms(&p.finished_at),
                updated_at: Some(now.timestamp_millis().to_string()),
                ..self.base.clone()
            }
        });
        match self.server.write_runtime(&doc) {
            Ok(true) => {}
            Ok(false) => {
                tracing::debug!(component = "indexing", event = "runtime_fenced", source_id = %doc.source_id)
            }
            Err(e) => {
                tracing::debug!(component = "indexing", event = "runtime_write_failed", source_id = %doc.source_id, error = %e)
            }
        }
    }
}

/// Forwards coordinator events and mirrors them into the heartbeat.
struct Reporting<'a, 'b> {
    inner: &'a mut dyn Progress,
    beat: &'a RuntimeHeartbeat<'b>,
}

impl Progress for Reporting<'_, '_> {
    fn event(&mut self, event: &ProgressEvent) {
        self.inner.event(event);
        self.beat.apply(event);
    }
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
        stage: "publishing",
    });
    let build_id = record::build_id(
        &source.id,
        &format!("{}-{}-{}", now_iso(), std::process::id(), lease.token),
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
        seconds = report.seconds,
    );
    result.server_publish = Some(serde_json::to_value(&report).map_err(generic)?);
    Ok(result)
}

/// What a server-mode graph stage did.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ServerGraphRun {
    pub graph: ragmonk_code::graph_stage::GraphReport,
    pub publish: ragmonk_backends::graph::GraphPublishReport,
}

/// Why a server-mode graph stage did not publish.
#[derive(Debug)]
pub enum ServerGraphError {
    /// Another process holds the source's writer lease.
    Blocked(RagMonkError),
    Graph(ragmonk_code::graph_stage::GraphError),
    Other(RagMonkError),
}

/// The relationship graph stage of one server-mode source, after its base
/// was published: derive the graph in the staging store (which must
/// describe the server's active build) and publish it as a new graph
/// generation, fenced by the writer lease and the expected active base. The
/// caller holds the source's local lock.
pub fn build_relationships(
    home: &Home,
    cfg: &RagMonkConfig,
    server: &ServerBackend,
    source: &SourceRecord,
    opts: &Options,
    progress: &mut ragmonk_code::graph_stage::GraphProgressFn<'_>,
) -> Result<ServerGraphRun, ServerGraphError> {
    use ragmonk_code::graph_stage::GraphError;
    let ttl = Duration::from_secs_f64(cfg.storage.server.lease_seconds);
    let lease = server
        .acquire_lease(&source.id, &lease_owner(), ttl)
        .map_err(|e| ServerGraphError::Blocked(server_err(e)))?;
    let stop = AtomicBool::new(false);
    let lost = AtomicBool::new(false);
    let result = std::thread::scope(|s| {
        let (stop_ref, lost_ref, lease_ref) = (&stop, &lost, &lease);
        s.spawn(move || {
            let step = (ttl / 3).max(Duration::from_millis(500));
            let mut waited = Duration::ZERO;
            while !stop_ref.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(100));
                waited += Duration::from_millis(100);
                if waited >= step {
                    waited = Duration::ZERO;
                    if server.renew_lease(lease_ref).is_err() {
                        lost_ref.store(true, Ordering::SeqCst);
                        return;
                    }
                }
            }
        });
        let _renewal = Renewal { stop: &stop };
        graph_pass(home, cfg, server, source, opts, &lease, &lost, progress)
    });
    match &result {
        Err(ServerGraphError::Graph(GraphError::Stale(reason))) => {
            let _ = server.set_graph_state(&source.id, Some(&lease), "stale", None, Some(reason));
        }
        Err(ServerGraphError::Graph(e)) => {
            let _ = server.set_graph_state(
                &source.id,
                Some(&lease),
                "failed",
                Some(&e.to_string()),
                None,
            );
        }
        _ => {}
    }
    let _ = server.release_lease(&lease);
    result
}

#[allow(clippy::too_many_arguments)]
fn graph_pass(
    home: &Home,
    cfg: &RagMonkConfig,
    server: &ServerBackend,
    source: &SourceRecord,
    opts: &Options,
    lease: &Lease,
    lost: &AtomicBool,
    progress: &mut ragmonk_code::graph_stage::GraphProgressFn<'_>,
) -> Result<ServerGraphRun, ServerGraphError> {
    use ragmonk_code::graph_stage::GraphError;
    let other = ServerGraphError::Other;
    let root = staging_root(home, cfg);
    let layout = StorageLayout::at(&root);
    let server_active = server
        .source_state(&source.id)
        .map_err(|e| other(server_err(e)))?
        .and_then(|s| s.active_build_id);
    let Some(server_active) = server_active else {
        return Err(ServerGraphError::Graph(GraphError::NotPublished));
    };
    let synced = std::fs::read_to_string(marker(&root, &source.id))
        .ok()
        .map(|s| s.trim().to_owned());
    if synced.as_deref() != Some(server_active.as_str()) {
        return Err(ServerGraphError::Graph(GraphError::Stale(
            "the local staging copy does not describe the server's active build; run 'ragmonk index' first"
                .into(),
        )));
    }
    let cp = open_staging(&layout, cfg).map_err(other)?;
    let local_build = cp
        .state(&source.id)
        .map_err(|e| other(db(e)))?
        .active_build_id
        .ok_or(ServerGraphError::Graph(GraphError::NotPublished))?;
    let mut store = ProjectStore::open(
        &layout,
        &project_id_for_canonical(&source.path),
        &source.id,
        cfg.runtime.sqlite_cache_size_mb,
    )
    .map_err(|e| other(db(e)))?;
    let graph = ragmonk_knowledge::build_graph(
        &mut store,
        std::path::Path::new(&source.path),
        &local_build,
        opts.workers,
        Some(lost),
        progress,
    )
    .map_err(ServerGraphError::Graph)?;
    let generation = record::build_id(
        &source.id,
        &format!("graph-{}-{}-{}", now_iso(), std::process::id(), lease.token),
    );
    let publish = server
        .publish_graph(&ragmonk_backends::graph::GraphPublishInput {
            source_id: &source.id,
            store: &store,
            local_build: &local_build,
            base_build_id: &server_active,
            lease: Some(lease),
            generation: &generation,
        })
        .map_err(|e| match e {
            ragmonk_backends::BackendError::Conflict(m) => {
                ServerGraphError::Graph(GraphError::Stale(m))
            }
            e => ServerGraphError::Graph(GraphError::Failed(
                ragmonk_telemetry::redact::redact_urls_in_text(&e.to_string()),
            )),
        })?;
    tracing::info!(
        component = "indexing",
        event = "server_graph_publish",
        source_id = %source.id,
        published = publish.published,
        edges = publish.edges_written,
        links = publish.links_written,
        seconds = publish.seconds,
    );
    Ok(ServerGraphRun { graph, publish })
}

/// Marks a server source's graph disabled (hidden, never deleted). A
/// source whose lease is held elsewhere is left for its holder's next run.
pub fn mark_relationships_disabled(
    cfg: &RagMonkConfig,
    server: &ServerBackend,
    source: &SourceRecord,
) -> Result<(), RagMonkError> {
    let Some((graph, _)) = server.graph(&source.id).map_err(server_err)? else {
        return Ok(());
    };
    if graph.state == "disabled" {
        return Ok(());
    }
    let ttl = Duration::from_secs_f64(cfg.storage.server.lease_seconds);
    let lease = server
        .acquire_lease(&source.id, &lease_owner(), ttl)
        .map_err(server_err)?;
    let r = server
        .set_graph_state(&source.id, Some(&lease), "disabled", None, None)
        .map_err(server_err);
    let _ = server.release_lease(&lease);
    r
}
