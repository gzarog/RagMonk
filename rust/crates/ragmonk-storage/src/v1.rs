//! Read-only access to Python V1 data in a RagMonk home.
//!
//! Only source *definitions* are ever imported. Everything else is
//! inventoried for the preflight report and deliberately ignored.

use std::path::Path;

use rusqlite::Connection;
use serde::Serialize;

use ragmonk_core::paths::{project_id_for_canonical, Home};

use crate::db::open_read_only;
use crate::error::{Result, StorageError};

/// A source definition from the Python `sources.db`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct V1Source {
    pub id: String,
    pub path: String,
    pub source_type: String,
    pub enabled: bool,
    pub include_patterns: Vec<String>,
    pub exclude_patterns: Vec<String>,
    pub status: Option<String>,
    pub created_at: String,
    /// Path-derived project directory name (`sha256(path)[:12]`).
    pub project_id: String,
}

/// Row counts of index-derived V1 state for one source (ignored by V2).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct V1ProjectInventory {
    pub project_id: String,
    pub db_present: bool,
    pub schema_version: Option<i64>,
    /// `(table, rows)` for every known index-derived table that exists.
    pub tables: Vec<(String, i64)>,
}

/// Tables of the V1 knowledge.db that hold index-derived state.
pub const V1_INDEX_DERIVED_TABLES: &[&str] = &[
    "files",
    "index_jobs",
    "index_errors",
    "entities",
    "relationships",
    "documents",
    "document_sections",
    "cross_links",
    "embeddings",
    "vector_items",
    "document_conversion_cache",
    "embedding_cache",
];

fn table_exists(conn: &Connection, name: &str) -> Result<bool> {
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
            [name],
            |r| r.get(0),
        )
        .map_err(StorageError::sqlite("inspect v1 schema"))?;
    Ok(n > 0)
}

fn column_exists(conn: &Connection, table: &str, column: &str) -> Result<bool> {
    let n: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM pragma_table_info(?1) WHERE name = ?2",
            [table, column],
            |r| r.get(0),
        )
        .map_err(StorageError::sqlite("inspect v1 schema"))?;
    Ok(n > 0)
}

fn schema_version(conn: &Connection) -> Result<Option<i64>> {
    if !table_exists(conn, "schema_migrations")? {
        return Ok(None);
    }
    conn.query_row("SELECT MAX(version) FROM schema_migrations", [], |r| {
        r.get(0)
    })
    .map_err(StorageError::sqlite("read v1 schema version"))
}

/// Reads V1 source definitions. Missing `sources.db` means no V1 data.
pub fn read_sources(home: &Home) -> Result<Option<(Option<i64>, Vec<V1Source>)>> {
    let path = home.sources_db();
    if !path.is_file() {
        return Ok(None);
    }
    let conn = open_read_only(&path)?;
    if !table_exists(&conn, "sources")? {
        return Ok(Some((schema_version(&conn)?, Vec::new())));
    }
    let has_status = column_exists(&conn, "sources", "status")?;
    let sql = format!(
        "SELECT id, path, source_type, enabled, include_patterns, exclude_patterns, {}, created_at
         FROM sources ORDER BY created_at, id",
        if has_status { "status" } else { "NULL" }
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(StorageError::sqlite("read v1 sources"))?;
    let rows = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, Option<String>>(6)?,
                r.get::<_, String>(7)?,
            ))
        })
        .map_err(StorageError::sqlite("read v1 sources"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(StorageError::sqlite("read v1 sources"))?;
    let list = |s: &str| -> Vec<String> { serde_json::from_str(s).unwrap_or_default() };
    let sources = rows
        .into_iter()
        .map(
            |(id, path, source_type, enabled, inc, exc, status, created_at)| V1Source {
                project_id: project_id_for_canonical(&path),
                id,
                path,
                source_type,
                enabled: enabled != 0,
                include_patterns: list(&inc),
                exclude_patterns: list(&exc),
                status,
                created_at,
            },
        )
        .collect();
    Ok(Some((schema_version(&conn)?, sources)))
}

/// Inventories one V1 project database (read-only).
pub fn inventory_project(home: &Home, project_id: &str) -> Result<V1ProjectInventory> {
    let path = home.project_db(project_id);
    inventory_db(&path, project_id)
}

fn inventory_db(path: &Path, project_id: &str) -> Result<V1ProjectInventory> {
    if !path.is_file() {
        return Ok(V1ProjectInventory {
            project_id: project_id.to_owned(),
            db_present: false,
            schema_version: None,
            tables: Vec::new(),
        });
    }
    let conn = open_read_only(path)?;
    let mut tables = Vec::new();
    for t in V1_INDEX_DERIVED_TABLES {
        if table_exists(&conn, t)? {
            let n: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM \"{t}\""), [], |r| r.get(0))
                .map_err(StorageError::sqlite("count v1 rows"))?;
            tables.push(((*t).to_owned(), n));
        }
    }
    Ok(V1ProjectInventory {
        project_id: project_id.to_owned(),
        db_present: true,
        schema_version: schema_version(&conn)?,
        tables,
    })
}
