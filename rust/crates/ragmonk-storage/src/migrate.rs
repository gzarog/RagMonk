//! Ordered, versioned, idempotent schema migrations with automatic backup.
//!
//! * Each migration runs in one `BEGIN IMMEDIATE` transaction together with
//!   its `schema_migrations` row, so a failure leaves the previous version
//!   intact.
//! * Before migrating a database that already holds data, a consistent copy
//!   is written (`VACUUM INTO`) into the V2 migration
//!   backup directory (preflight + backup rule for destructive changes).
//! * A database whose recorded version is newer than this build knows is
//!   refused rather than downgraded.

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OptionalExtension};

use crate::db::{now_iso, write_tx};
use crate::error::{Result, StorageError};

#[derive(Debug, Clone, Copy)]
pub struct Migration {
    pub version: i64,
    pub name: &'static str,
    pub sql: &'static str,
}

fn ensure_table(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )",
    )
    .map_err(StorageError::sqlite("create schema_migrations"))
}

pub fn current_version(conn: &Connection) -> Result<i64> {
    ensure_table(conn)?;
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        [],
        |r| r.get(0),
    )
    .map_err(StorageError::sqlite("read schema version"))
}

/// Migrations not yet applied, in order.
pub fn pending<'a>(conn: &Connection, migrations: &'a [Migration]) -> Result<Vec<&'a Migration>> {
    let current = current_version(conn)?;
    Ok(migrations.iter().filter(|m| m.version > current).collect())
}

fn has_user_data(conn: &Connection) -> Result<bool> {
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'
             AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations'",
            [],
            |r| r.get(0),
        )
        .map_err(StorageError::sqlite("inspect schema"))?;
    Ok(count > 0)
}

/// Writes a consistent backup copy of `conn` into `dir`.
pub fn backup(conn: &Connection, dir: &Path, label: &str) -> Result<PathBuf> {
    std::fs::create_dir_all(dir)
        .map_err(|e| StorageError::Io(format!("cannot create {}: {e}", dir.display())))?;
    let stamp = chrono::Utc::now().format("%Y%m%dT%H%M%S%6fZ");
    let path = dir.join(format!("{label}-{stamp}.db"));
    // VACUUM INTO writes a consistent, compacted copy without holding a
    // write lock on the source for the duration.
    conn.execute("VACUUM INTO ?1", [path.to_string_lossy().as_ref()])
        .map_err(StorageError::sqlite(format!(
            "backup to {}",
            path.display()
        )))?;
    Ok(path)
}

/// Outcome of [`apply`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Applied {
    pub from: i64,
    pub to: i64,
    pub backup: Option<PathBuf>,
}

/// Applies pending migrations. `backup_dir` is used when the database
/// already holds data and at least one migration is pending.
pub fn apply(
    conn: &mut Connection,
    db_label: &str,
    migrations: &[Migration],
    backup_dir: &Path,
) -> Result<Applied> {
    let from = current_version(conn)?;
    let supported = migrations.iter().map(|m| m.version).max().unwrap_or(0);
    if from > supported {
        return Err(StorageError::SchemaTooNew {
            db: db_label.to_owned(),
            found: from,
            supported,
        });
    }
    let todo: Vec<Migration> = migrations
        .iter()
        .filter(|m| m.version > from)
        .copied()
        .collect();
    if todo.is_empty() {
        return Ok(Applied {
            from,
            to: from,
            backup: None,
        });
    }
    let backup = if has_user_data(conn)? {
        Some(backup(conn, backup_dir, &format!("{db_label}-v{from}"))?)
    } else {
        None
    };
    for m in &todo {
        write_tx(conn, |tx| {
            tx.execute_batch(m.sql)
                .map_err(StorageError::sqlite(format!(
                    "migration {} ({})",
                    m.version, m.name
                )))?;
            let violations: Option<i64> = tx
                .query_row("SELECT 1 FROM pragma_foreign_key_check LIMIT 1", [], |r| {
                    r.get(0)
                })
                .optional()
                .map_err(StorageError::sqlite("foreign_key_check"))?;
            if violations.is_some() {
                return Err(StorageError::Invalid(format!(
                    "migration {} ({}) left foreign key violations",
                    m.version, m.name
                )));
            }
            tx.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?1, ?2, ?3)",
                rusqlite::params![m.version, m.name, now_iso()],
            )
            .map_err(StorageError::sqlite("record migration"))?;
            Ok(())
        })?;
    }
    Ok(Applied {
        from,
        to: todo.last().map_or(from, |m| m.version),
        backup,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const M1: Migration = Migration {
        version: 1,
        name: "one",
        sql: "CREATE TABLE t(a INTEGER);",
    };
    const M2: Migration = Migration {
        version: 2,
        name: "two",
        sql: "ALTER TABLE t ADD COLUMN b TEXT;",
    };
    const BAD: Migration = Migration {
        version: 3,
        name: "bad",
        sql: "ALTER TABLE nope ADD COLUMN x;",
    };

    #[test]
    fn applies_idempotently_backs_up_and_rolls_back_failures() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = crate::db::open(&dir.path().join("x.db"), 8).unwrap();
        let a = apply(&mut conn, "x", &[M1], dir.path()).unwrap();
        assert_eq!((a.from, a.to, a.backup.is_none()), (0, 1, true));
        conn.execute("INSERT INTO t(a) VALUES (7)", []).unwrap();
        let a = apply(&mut conn, "x", &[M1, M2], &dir.path().join("bk")).unwrap();
        assert_eq!((a.from, a.to), (1, 2));
        let bk = a.backup.expect("existing data must be backed up");
        let copy = Connection::open(bk).unwrap();
        let n: i64 = copy
            .query_row("SELECT COUNT(*) FROM t", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
        // Re-running is a no-op.
        assert_eq!(apply(&mut conn, "x", &[M1, M2], dir.path()).unwrap().to, 2);
        // A failing migration leaves version 2 intact.
        assert!(apply(&mut conn, "x", &[M1, M2, BAD], dir.path()).is_err());
        assert_eq!(current_version(&conn).unwrap(), 2);
    }

    #[test]
    fn refuses_newer_schema() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = crate::db::open(&dir.path().join("x.db"), 8).unwrap();
        apply(&mut conn, "x", &[M1, M2], dir.path()).unwrap();
        let err = apply(&mut conn, "x", &[M1], dir.path()).unwrap_err();
        assert!(matches!(
            err,
            StorageError::SchemaTooNew {
                found: 2,
                supported: 1,
                ..
            }
        ));
    }
}
