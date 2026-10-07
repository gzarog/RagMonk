//! Source registration: add, list, inspect, enable/disable and remove.

use std::path::Path;

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::daemon::pid;
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::control::{ControlPlane, NewSource, SourceRecord};
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::StorageLayout;
use serde_json::{json, Value};

use crate::{db, generic, load};

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

pub fn source_info(cp: &ControlPlane, s: &SourceRecord) -> Result<Value, RagMonkError> {
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

pub fn assert_no_active_daemon(home: &Home) -> Result<(), RagMonkError> {
    if pid::running_daemon(home).is_some() {
        return Err(RagMonkError::usage(
            "a RagMonk daemon is running and may be actively indexing sources; \
             run `ragmonk daemon stop` first, then retry.",
        ));
    }
    Ok(())
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

/// Where a source's indexed data lives (never its original files).
pub fn project_data_dir(home: &Home, source: &SourceRecord) -> std::path::PathBuf {
    StorageLayout::new(home).project_dir(&project_id_for_canonical(&source.path))
}
