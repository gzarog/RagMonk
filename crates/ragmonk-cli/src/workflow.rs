//! Core workflow commands (RUST-12 slice 1): `init`, `source …`, `index`,
//! `docs` and `watch`. Messages and `--json` payloads match the Python
//! CLI. Rich markup is dropped from text output, and Rich tables become
//! plain aligned columns.

use std::path::Path;

use clap::Subcommand;
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::coordinator::{run_source, Options};
use ragmonk_indexing::daemon::pid;
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::control::{ControlPlane, NewSource, SourceRecord};
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::StorageLayout;
use serde_json::{json, Value};

use crate::{load, prepared_home, print_json};

fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

fn db(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

/// The control plane (created on first use).
pub fn control_plane(home: &Home) -> Result<ControlPlane, RagMonkError> {
    let cfg = load(home)?;
    ControlPlane::open(&StorageLayout::new(home), cfg.runtime.sqlite_cache_size_mb).map_err(db)
}

pub fn get_source(cp: &ControlPlane, id: &str) -> Result<SourceRecord, RagMonkError> {
    cp.get_source(id)
        .map_err(db)?
        .ok_or_else(|| RagMonkError::usage(format!("no such source: {id}")))
}

/// Plain aligned table: a header row, then rows, columns separated by two
/// spaces.
pub fn print_table(headers: &[&str], rows: &[Vec<String>]) {
    let mut widths: Vec<usize> = headers.iter().map(|h| h.chars().count()).collect();
    for r in rows {
        for (i, c) in r.iter().enumerate() {
            if let Some(w) = widths.get_mut(i) {
                *w = (*w).max(c.chars().count());
            }
        }
    }
    let line = |cells: Vec<&str>| {
        let s: Vec<String> = cells
            .iter()
            .enumerate()
            .map(|(i, c)| format!("{c:<w$}", w = widths[i]))
            .collect();
        println!("{}", s.join("  ").trim_end());
    };
    line(headers.to_vec());
    for r in rows {
        line(r.iter().map(String::as_str).collect());
    }
}

// ---------------------------------------------------------------- init ---

#[derive(clap::Args)]
pub struct InitArgs {
    /// Storage backend mode for a *new* config: 'local' (default) or 'server'.
    #[arg(long = "storage-mode", default_value = "local")]
    storage_mode: String,
    /// Shorthand for --storage-mode=local (the default; explicit opt-in).
    #[arg(long)]
    local: bool,
    /// Server engine when --storage-mode=server: 'opensearch' or 'elasticsearch'.
    #[arg(long = "storage-engine", default_value = "opensearch")]
    storage_engine: String,
    /// Server URL when --storage-mode=server.
    #[arg(long = "storage-url", default_value = "")]
    storage_url: String,
    /// Index name prefix when --storage-mode=server.
    #[arg(long = "storage-index-prefix", default_value = "ragmonk")]
    storage_index_prefix: String,
    /// Do not verify TLS certificates when --storage-mode=server.
    #[arg(long = "storage-no-verify-tls")]
    storage_no_verify_tls: bool,
}

fn validated_server_config(
    a: &InitArgs,
) -> Result<ragmonk_config::model::ServerStorageConfig, RagMonkError> {
    if a.storage_url.is_empty() {
        return Err(RagMonkError::usage(
            "--storage-url is required when --storage-mode=server",
        ));
    }
    if ragmonk_telemetry::redact::url_has_userinfo(&a.storage_url) {
        return Err(RagMonkError::usage(
            "--storage-url must not contain credentials (user-info such as 'user:password@'); \
             set RAGMONK_OPENSEARCH_USERNAME/_PASSWORD/_API_KEY or \
             RAGMONK_ELASTICSEARCH_USERNAME/_PASSWORD/_API_KEY instead; \
             no config file was written",
        ));
    }
    if !matches!(a.storage_engine.as_str(), "opensearch" | "elasticsearch") {
        return Err(RagMonkError::usage(format!(
            "unknown --storage-engine {:?}; expected 'opensearch' or 'elasticsearch'",
            a.storage_engine
        )));
    }
    let mut cfg = ragmonk_config::RagMonkConfig::default();
    let server = &mut cfg.storage.server;
    server.engine = if a.storage_engine == "elasticsearch" {
        ragmonk_config::model::StorageEngine::Elasticsearch
    } else {
        ragmonk_config::model::StorageEngine::OpenSearch
    };
    server.url = a.storage_url.clone();
    server.index_prefix = a.storage_index_prefix.clone();
    server.verify_tls = !a.storage_no_verify_tls;
    let server = cfg.storage.server;
    let redacted = ragmonk_telemetry::redact::redact_urls_in_text(&a.storage_url);
    ragmonk_backends::ServerBackend::connect(&server, None).map_err(|e| {
        RagMonkError::usage(format!(
            "storage validation failed: {}; no config file was written",
            ragmonk_telemetry::redact::redact_urls_in_text(&e.to_string())
        ))
    })?;
    println!(
        "Storage validation OK -- {} at {redacted}",
        a.storage_engine
    );
    Ok(server)
}

pub fn init(a: &InitArgs) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let config_path = home.user_config();
    if config_path.is_file() {
        // Validates the existing config; errors surface early.
        load(&home)?;
        println!("Using existing configuration at {}", config_path.display());
    } else {
        if a.local && a.storage_mode != "local" {
            return Err(RagMonkError::usage(
                "--local conflicts with --storage-mode=server",
            ));
        }
        let mut cfg = ragmonk_config::RagMonkConfig::default();
        match a.storage_mode.as_str() {
            "local" => {}
            "server" => {
                cfg.storage.mode = "server".into();
                cfg.storage.server = validated_server_config(a)?;
            }
            other => {
                return Err(RagMonkError::usage(format!(
                    "unknown --storage-mode {other:?}; expected 'local' or 'server'"
                )))
            }
        }
        ragmonk_config::write_user_config(&cfg, &home).map_err(generic)?;
        println!("Wrote default configuration to {}", config_path.display());
    }
    control_plane(&home)?;
    println!("RagMonk initialized at {}", home.root().display());
    Ok(())
}

// -------------------------------------------------------------- source ---

#[derive(Subcommand)]
pub enum SourceCommand {
    /// Register a directory as a source.
    Add {
        path: String,
        /// Include glob pattern.
        #[arg(long)]
        include: Vec<String>,
        /// Exclude glob pattern.
        #[arg(long)]
        exclude: Vec<String>,
    },
    /// List registered sources.
    List,
    /// Show one source as JSON.
    Info { source_id: String },
    /// Enable a source.
    Enable { source_id: String },
    /// Disable a source.
    Disable { source_id: String },
    /// Remove a source and its indexed data (never the original files).
    Remove {
        source_id: String,
        /// Skip the confirmation prompt.
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

fn expand_user(raw: &str) -> std::path::PathBuf {
    if let Some(rest) = raw.strip_prefix("~/").or_else(|| raw.strip_prefix("~\\")) {
        if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
            return Path::new(&home).join(rest);
        }
    }
    if raw == "~" {
        if let Some(home) = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }) {
            return home.into();
        }
    }
    raw.into()
}

fn source_info(cp: &ControlPlane, s: &SourceRecord) -> Result<Value, RagMonkError> {
    let state = cp.state(&s.id).map_err(db)?;
    Ok(json!({
        "id": s.id,
        "path": s.path,
        "source_type": s.source_type.as_str(),
        "enabled": s.enabled,
        "include_patterns": s.include_patterns,
        "exclude_patterns": s.exclude_patterns,
        "status": state.online_status,
        "build_state": state.build_state.as_str(),
        "rebuild_reason": state.rebuild_reason,
        "active_build_id": state.active_build_id,
        "last_full_build_at": state.last_full_build_at,
        "last_scan_at": state.last_scan_at,
        "last_error": state.last_error,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }))
}

fn assert_no_active_daemon(home: &Home) -> Result<(), RagMonkError> {
    if pid::running_daemon(home).is_some() {
        return Err(RagMonkError::usage(
            "a RagMonk daemon is running and may be actively indexing sources; \
             run `ragmonk daemon stop` first, then retry.",
        ));
    }
    Ok(())
}

fn confirm(prompt: &str) -> bool {
    use std::io::Write;
    print!("{prompt} [y/N]: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    if std::io::stdin().read_line(&mut line).is_err() {
        return false;
    }
    matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes")
}

/// Registers a source (`SourceRegistry.add`).
pub fn add_source(
    cp: &mut ControlPlane,
    path: &str,
    include: Vec<String>,
    exclude: Vec<String>,
) -> Result<SourceRecord, RagMonkError> {
    let source_type = ragmonk_core::ids::detect_source_type(path);
    let p = expand_user(path);
    if !p.exists() {
        return Err(RagMonkError::new(
            ErrorKind::SourceUnavailable,
            format!("source path does not exist: {path}"),
        ));
    }
    let canonical = ragmonk_core::paths::resolve(&p)
        .map_err(generic)?
        .to_string_lossy()
        .into_owned();
    if !p.is_dir() {
        return Err(RagMonkError::new(
            ErrorKind::SourceUnavailable,
            format!("source path is not a directory: {canonical}"),
        ));
    }
    let (s, _) = cp
        .add_source(&NewSource {
            canonical_path: canonical,
            source_type,
            enabled: true,
            include_patterns: include,
            exclude_patterns: exclude,
        })
        .map_err(db)?;
    Ok(s)
}

/// Enables or disables a registered source.
pub fn set_source_enabled(
    cp: &mut ControlPlane,
    source_id: &str,
    enabled: bool,
) -> Result<(), RagMonkError> {
    get_source(cp, source_id)?;
    cp.set_enabled(source_id, enabled).map_err(db)
}

/// Removes a source and its indexed data (never the original files),
/// under the `index` lock and only while no daemon is running. Returns
/// whether indexed data was deleted.
pub fn remove_source(home: &Home, source_id: &str) -> Result<bool, RagMonkError> {
    let mut cp = control_plane(home)?;
    let s = get_source(&cp, source_id)?;
    assert_no_active_daemon(home)?;
    let layout = StorageLayout::new(home);
    let project_dir = layout.project_dir(&project_id_for_canonical(&s.path));
    let cfg = load(home)?;
    let lock = RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        "source-remove",
        Some(source_id),
        std::time::Duration::from_secs_f64(cfg.indexing.lock_timeout_seconds),
    )?;
    // Data first: if deleting it fails, the source stays registered
    // rather than half removed.
    let deleted = project_dir.exists();
    if deleted {
        std::fs::remove_dir_all(&project_dir).map_err(|e| {
            generic(format!(
                "failed to delete project data at {}: {e}; source '{source_id}' was not removed",
                project_dir.display()
            ))
        })?;
    }
    cp.remove_source(source_id).map_err(db)?;
    lock.release();
    Ok(deleted)
}

pub fn source(cmd: SourceCommand) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let mut cp = control_plane(&home)?;
    match cmd {
        SourceCommand::Add {
            path,
            include,
            exclude,
        } => {
            let s = add_source(&mut cp, &path, include, exclude)?;
            println!("Added source {} -> {}", s.id, s.path);
        }
        SourceCommand::List => {
            let mut rows = Vec::new();
            for s in cp.list_sources(false).map_err(db)? {
                let state = cp.state(&s.id).map_err(db)?;
                rows.push(vec![
                    s.id.clone(),
                    s.path.clone(),
                    s.source_type.as_str().into(),
                    if s.enabled { "yes" } else { "no" }.into(),
                    state.online_status.clone(),
                    state.last_scan_at.clone().unwrap_or_else(|| "-".into()),
                ]);
            }
            print_table(
                &["ID", "Path", "Type", "Enabled", "Status", "Last Scan"],
                &rows,
            );
        }
        SourceCommand::Info { source_id } => {
            let s = get_source(&cp, &source_id)?;
            let v = source_info(&cp, &s)?;
            println!("{}", serde_json::to_string_pretty(&v).map_err(generic)?);
        }
        SourceCommand::Enable { source_id } => {
            set_source_enabled(&mut cp, &source_id, true)?;
            println!("Enabled {source_id}");
        }
        SourceCommand::Disable { source_id } => {
            set_source_enabled(&mut cp, &source_id, false)?;
            println!("Disabled {source_id}");
        }
        SourceCommand::Remove { source_id, yes } => {
            let s = get_source(&cp, &source_id)?;
            assert_no_active_daemon(&home)?;
            let layout = StorageLayout::new(&home);
            let project_dir = layout.project_dir(&project_id_for_canonical(&s.path));
            if !yes {
                println!("This will permanently remove:");
                println!("  - source {} ({}) from the registry", s.id, s.path);
                println!("  - its indexed data at {}", project_dir.display());
                println!("The original files at the source path are never touched.");
                if !confirm("Proceed?") {
                    println!("Aborted; nothing was removed.");
                    return Ok(());
                }
            }
            let deleted = remove_source(&home, &source_id)?;
            println!("Removed {} ({})", s.id, s.path);
            if deleted {
                println!("  deleted indexed data at {}", project_dir.display());
            }
        }
    }
    Ok(())
}

// --------------------------------------------------------------- index ---

pub fn index(source_id: Option<String>) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let cfg = load(&home)?;
    let mut cp = control_plane(&home)?;
    let sources = match &source_id {
        Some(id) => vec![get_source(&cp, id)?],
        None => cp.list_sources(true).map_err(db)?,
    };
    if sources.is_empty() {
        println!("No enabled sources to index.");
        return Ok(());
    }
    let layout = StorageLayout::new(&home);
    let registry =
        ragmonk_convert::registry_with(&cfg, &ragmonk_convert::RegistryOptions::for_home(&home));
    let opts = Options::from_config(&cfg);
    let mut total_failed = 0usize;
    let mut failed_sources = 0usize;
    let total = sources.len() as i64;
    let tracked: Result<(), RagMonkError> = ragmonk_indexing::progress::track(
        &home.index_progress(),
        "index",
        Some(total),
        |tracker| {
            for (i, source) in sources.iter().enumerate() {
                tracker.begin_source(&source.id, Some(i as i64 + 1));
                let lock = match RunLock::acquire(
                    &home.locks_dir().join("index.lock"),
                    "index",
                    Some(&source.id),
                    opts.lock_timeout,
                ) {
                    Ok(l) => l,
                    Err(e) => {
                        failed_sources += 1;
                        println!("{} {}: blocked: {}", source.id, source.path, e.message());
                        continue;
                    }
                };
                let outcome = run_source(&layout, &mut cp, source, &registry, &opts, tracker);
                lock.release();
                let r = match outcome {
                    Ok(r) => r,
                    Err(e) => {
                        failed_sources += 1;
                        println!(
                            "{} {}: source pass failed: {}",
                            source.id,
                            source.path,
                            ragmonk_telemetry::redact::redact_urls_in_text(e.message())
                        );
                        continue;
                    }
                };
                if let Some(reason) = &r.offline {
                    println!(
                        "{} {}: source unreachable ({reason}); marked OFFLINE, skipped deletion reconciliation",
                        source.id, source.path
                    );
                    continue;
                }
                total_failed += r.failed;
                if r.became_online {
                    println!(
                        "{} {}: source reachable again; back to ACTIVE",
                        source.id, source.path
                    );
                }
                let c = &r.counts;
                println!(
                    "{} {}: scanned={} new={} changed={} unchanged={} moved={} deleted={} \
                     indexed={} skipped={} failed={} linked={} embedded={}",
                    source.id,
                    source.path,
                    c.scanned,
                    c.new,
                    c.changed,
                    c.unchanged,
                    c.moved,
                    c.deleted,
                    r.indexed,
                    r.skipped_limit,
                    r.failed,
                    r.linked,
                    r.embedded
                );
                let a = &r.attachments;
                if a.seen > 0 {
                    println!(
                        "{}: email attachments seen={} indexed={} skipped={} failed={}",
                        source.id, a.seen, a.indexed, a.skipped, a.failed
                    );
                }
                if !r.scan_errors.is_empty() {
                    println!(
                        "{}: scan was incomplete ({} unreadable path(s)); deletion reconciliation skipped this pass, will retry",
                        source.id,
                        r.scan_errors.len()
                    );
                }
            }
            Ok(())
        },
    );
    tracked?;
    let attempted = sources.len();
    if failed_sources > 0 {
        println!(
            "Index complete with failures: {attempted} source(s) attempted, {} completed, {failed_sources} source(s) failed.",
            attempted - failed_sources
        );
    } else {
        println!(
            "Index complete: {attempted} source(s) processed, {attempted} succeeded, 0 failed."
        );
    }
    if failed_sources > 0 || total_failed > 0 {
        let mut parts = Vec::new();
        if failed_sources > 0 {
            parts.push(format!("{failed_sources} source(s) failed"));
        }
        if total_failed > 0 {
            parts.push(format!("{total_failed} file(s) failed to index"));
        }
        return Err(RagMonkError::new(
            ErrorKind::IndexingPartialFailure,
            format!("{}; see 'ragmonk doctor' for details", parts.join("; ")),
        ));
    }
    Ok(())
}

// ---------------------------------------------------------------- docs ---

/// `docs` rows (`cli/docs._run`): every document-kind file of each
/// source's active build, with its document metadata when there is one.
pub fn docs_rows(home: &Home, source_id: Option<&str>) -> Result<Vec<Value>, RagMonkError> {
    let cfg = load(home)?;
    let cp = control_plane(home)?;
    let sources = match source_id {
        Some(id) => vec![get_source(&cp, id)?],
        None => cp.list_sources(false).map_err(db)?,
    };
    let layout = StorageLayout::new(home);
    let mut rows = Vec::new();
    for s in &sources {
        let Some(build) = cp.state(&s.id).map_err(db)?.active_build_id else {
            continue;
        };
        let store = ProjectStore::open(
            &layout,
            &project_id_for_canonical(&s.path),
            &s.id,
            cfg.runtime.sqlite_cache_size_mb,
        )
        .map_err(db)?;
        let docs = store.documents(&build).map_err(db)?;
        let counts = store.chunk_kind_counts(&build).map_err(db)?;
        let mut files = store.files(&build).map_err(db)?;
        files.sort_by(|a, b| a.rel_path.cmp(&b.rel_path));
        for f in files.iter().filter(|f| f.kind == "document") {
            let doc = docs
                .iter()
                .find(|d| d.file_id == f.id && d.attachment.is_none());
            let mut children: Vec<_> = docs
                .iter()
                .filter(|d| d.file_id == f.id && d.attachment.is_some())
                .collect();
            children.sort_by_key(|d| d.attachment.as_ref().map_or(0, |a| a.index));
            let attachments: Vec<Value> = children
                .iter()
                .map(|d| json!(d.attachment.as_ref().and_then(|a| a.name.clone())))
                .collect();
            let (sections, paragraphs, tables) = doc
                .and_then(|d| counts.get(&d.id).copied())
                .unwrap_or_default();
            let abs = Path::new(&s.path).join(&f.rel_path);
            rows.push(json!({
                "source_id": s.id,
                "file_id": f.id,
                "path": abs.to_string_lossy(),
                "status": f.status,
                "format": doc.map(|d| d.format.clone()),
                "title": doc.and_then(|d| d.title.clone()),
                "page_count": doc.and_then(|d| d.page_count),
                "section_count": sections,
                "paragraph_count": paragraphs,
                "table_count": tables,
                "is_scanned": doc.is_some_and(|d| d.is_scanned),
                "attachments": attachments,
            }));
        }
    }
    Ok(rows)
}

pub fn docs(source_id: Option<String>, json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let rows = docs_rows(&home, source_id.as_deref())?;
    if json_output {
        return print_json(&json!({ "documents": rows }));
    }
    if rows.is_empty() {
        println!("No indexed documents.");
        return Ok(());
    }
    let s = |v: &Value| v.as_str().map_or_else(|| "-".to_owned(), str::to_owned);
    let mut table = Vec::new();
    for r in &rows {
        table.push(vec![
            s(&r["path"]),
            s(&r["format"]),
            s(&r["status"]),
            r["page_count"]
                .as_i64()
                .map_or_else(|| "-".to_owned(), |n| n.to_string()),
            r["section_count"].to_string(),
            s(&r["title"]),
        ]);
        for a in r["attachments"].as_array().into_iter().flatten() {
            table.push(vec![
                format!("  -> {}", a.as_str().unwrap_or_default()),
                "attachment".into(),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
            ]);
        }
    }
    print_table(
        &["Path", "Format", "Status", "Pages", "Sections", "Title"],
        &table,
    );
    Ok(())
}
