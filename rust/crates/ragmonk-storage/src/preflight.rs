//! V1 → V2 control-plane preflight and (non-destructive) import.
//!
//! `preflight` is read-only and reports exactly what will be preserved
//! (source definitions, config) and what index-derived V1 state will be
//! ignored. `import_sources` copies source definitions into the V2 control
//! plane and marks every one as needing a full V2 rebuild. Neither touches
//! Python V1 files; deleting them (and legacy server indexes) is the
//! explicit, confirmed `migrate-to-rust-v2 --execute` step of a later phase.

use serde::Serialize;

use ragmonk_core::models::SourceType;
use ragmonk_core::paths::Home;

use crate::control::{BuildState, ControlPlane, NewSource, SourceOrigin};
use crate::error::Result;
use crate::schema::{CONTROL_MIGRATIONS, V2_SCHEMA_VERSION};
use crate::v1::{self, V1ProjectInventory, V1Source};
use crate::V2Layout;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SourcePlan {
    pub id: String,
    pub path: String,
    pub enabled: bool,
    pub path_exists: bool,
    /// Already present in the V2 control plane.
    pub already_imported: bool,
    /// What the first Rust V2 pass will do for this source.
    pub v2_action: &'static str,
    pub ignored_v1_state: V1ProjectInventory,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PreflightReport {
    pub home: String,
    pub config_file: String,
    pub config_file_present: bool,
    pub v1_sources_db_present: bool,
    pub v1_sources_schema_version: Option<i64>,
    pub v2_control_db: String,
    pub v2_control_present: bool,
    pub v2_control_schema_version: Option<i64>,
    pub v2_control_pending_migrations: Vec<String>,
    pub v2_schema_version: i64,
    pub sources: Vec<SourcePlan>,
    /// Preserved user data.
    pub preserved: Vec<&'static str>,
    /// Index-derived V1 state that V2 never reads.
    pub ignored: Vec<&'static str>,
    /// What import will write; nothing is deleted.
    pub writes: Vec<String>,
}

fn v2_action(enabled: bool, path_exists: bool) -> &'static str {
    match (enabled, path_exists) {
        (false, _) => "full_rebuild_when_enabled",
        (true, true) => "full_rebuild",
        (true, false) => "full_rebuild_when_path_available",
    }
}

/// Read-only: inspects V1 and V2 state without creating or changing files.
pub fn preflight(home: &Home) -> Result<PreflightReport> {
    let layout = V2Layout::new(home);
    let (v1_present, v1_version, v1_sources) = match v1::read_sources(home)? {
        Some((version, sources)) => (true, version, sources),
        None => (false, None, Vec::new()),
    };
    let control_path = layout.control_db();
    let control_present = control_path.is_file();
    let (control_version, pending, imported): (Option<i64>, Vec<String>, Vec<String>) =
        if control_present {
            let conn = crate::db::open_read_only(&control_path)?;
            let has_table: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'",
                [],
                |r| r.get(0),
            )
            .map_err(crate::StorageError::sqlite("inspect v2 control"))?;
            let version = if has_table > 0 {
                conn.query_row(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
                    [],
                    |r| r.get(0),
                )
                .map_err(crate::StorageError::sqlite("read v2 version"))?
            } else {
                0
            };
            let pending = CONTROL_MIGRATIONS
                .iter()
                .filter(|m| m.version > version)
                .map(|m| format!("{} ({})", m.version, m.name))
                .collect();
            let has_sources: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sources'",
                    [],
                    |r| r.get(0),
                )
                .map_err(crate::StorageError::sqlite("inspect v2 control"))?;
            let imported = if has_sources > 0 {
                let mut stmt = conn
                    .prepare("SELECT path FROM sources")
                    .map_err(crate::StorageError::sqlite("list v2 sources"))?;
                let rows = stmt
                    .query_map([], |r| r.get(0))
                    .map_err(crate::StorageError::sqlite("list v2 sources"))?;
                rows.collect::<rusqlite::Result<_>>()
                    .map_err(crate::StorageError::sqlite("list v2 sources"))?
            } else {
                Vec::new()
            };
            (Some(version), pending, imported)
        } else {
            (
                None,
                CONTROL_MIGRATIONS
                    .iter()
                    .map(|m| format!("{} ({})", m.version, m.name))
                    .collect(),
                Vec::new(),
            )
        };
    let mut sources = Vec::new();
    for s in &v1_sources {
        let exists = ControlPlane::path_exists(&s.path);
        sources.push(SourcePlan {
            id: s.id.clone(),
            path: s.path.clone(),
            enabled: s.enabled,
            path_exists: exists,
            already_imported: imported.contains(&s.path),
            v2_action: v2_action(s.enabled, exists),
            ignored_v1_state: v1::inventory_project(home, &s.project_id)?,
        });
    }
    let mut writes = vec![format!(
        "{} (created/migrated; backup first if it holds data)",
        control_path.display()
    )];
    let new_count = sources.iter().filter(|s| !s.already_imported).count();
    writes.push(format!(
        "{new_count} source definition(s) imported, each marked needs_full_rebuild"
    ));
    Ok(PreflightReport {
        home: home.root().display().to_string(),
        config_file: home.user_config().display().to_string(),
        config_file_present: home.user_config().is_file(),
        v1_sources_db_present: v1_present,
        v1_sources_schema_version: v1_version,
        v2_control_db: control_path.display().to_string(),
        v2_control_present: control_present,
        v2_control_schema_version: control_version,
        v2_control_pending_migrations: pending,
        v2_schema_version: V2_SCHEMA_VERSION,
        sources,
        preserved: vec![
            "config.yaml (read as-is; never rewritten by migration)",
            "source paths, enabled flags, include/exclude patterns",
        ],
        ignored: vec![
            "V1 per-file index state (files, hashes, status, generations)",
            "V1 jobs, errors, entities, relationships, documents, sections, links",
            "V1 embeddings, vector items and embedding/conversion caches",
            "V1 source scan state (last_scan_at, fingerprint, offline status)",
        ],
        writes,
    })
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ImportResult {
    pub imported: Vec<String>,
    pub already_present: Vec<String>,
    pub control_backup: Option<String>,
    pub needs_full_rebuild: Vec<String>,
}

fn v1_new_source(s: &V1Source) -> NewSource {
    NewSource {
        canonical_path: s.path.clone(),
        source_type: if s.source_type == "network" {
            SourceType::Network
        } else {
            SourceType::Local
        },
        enabled: s.enabled,
        include_patterns: s.include_patterns.clone(),
        exclude_patterns: s.exclude_patterns.clone(),
        origin: SourceOrigin::V1Import,
        created_at: Some(s.created_at.clone()),
    }
}

/// Imports V1 source definitions into the V2 control plane. Idempotent.
pub fn import_sources(home: &Home, cache_size_mb: i64) -> Result<ImportResult> {
    let layout = V2Layout::new(home);
    let (mut cp, applied) = ControlPlane::open(&layout, cache_size_mb)?;
    let v1_sources = v1::read_sources(home)?.map(|(_, s)| s).unwrap_or_default();
    let mut imported = Vec::new();
    let mut already = Vec::new();
    for s in &v1_sources {
        let (rec, created) = cp.add_source(&v1_new_source(s))?;
        if created {
            imported.push(rec.id);
        } else {
            already.push(rec.id);
        }
    }
    let mut needs = Vec::new();
    for rec in cp.list_sources(false)? {
        if cp.state(&rec.id)?.build_state == BuildState::NeedsFullRebuild {
            needs.push(rec.id);
        }
    }
    cp.log(
        "import_v1_sources",
        &serde_json::json!({ "imported": imported, "already_present": already }),
    )?;
    Ok(ImportResult {
        imported,
        already_present: already,
        control_backup: applied.backup.map(|p| p.display().to_string()),
        needs_full_rebuild: needs,
    })
}
