//! Read-only inspection and consistent snapshots of V2 databases, for
//! `backup`, `restore`, `upgrade` and `doctor`. Nothing here applies a
//! migration or writes to the database it inspects.

use std::path::Path;

use rusqlite::{Connection, OpenFlags};

use crate::schema::{CONTROL_MIGRATIONS, KNOWLEDGE_MIGRATIONS};

fn read_only(path: &Path) -> rusqlite::Result<Connection> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
}

/// Latest control-plane schema version this build knows.
pub fn latest_control_version() -> i64 {
    CONTROL_MIGRATIONS.last().map_or(0, |m| m.version)
}

/// Latest knowledge schema version this build knows.
pub fn latest_knowledge_version() -> i64 {
    KNOWLEDGE_MIGRATIONS.last().map_or(0, |m| m.version)
}

/// Applied schema version, without migrating: `None` when the file is
/// missing or unreadable, `0` when no migration was ever applied.
pub fn schema_version(path: &Path) -> Option<i64> {
    if !path.is_file() {
        return None;
    }
    let conn = read_only(path).ok()?;
    let has_table: bool = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .ok()?
        > 0;
    if !has_table {
        return Some(0);
    }
    conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        [],
        |r| r.get(0),
    )
    .ok()
}

/// `PRAGMA integrity_check`; the error describes what is wrong.
pub fn integrity_check(path: &Path) -> Result<(), String> {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let conn = read_only(path).map_err(|e| format!("cannot open {name}: {e}"))?;
    let result: String = conn
        .query_row("PRAGMA integrity_check", [], |r| r.get(0))
        .map_err(|e| format!("{name} is not a valid SQLite database: {e}"))?;
    if result == "ok" {
        Ok(())
    } else {
        Err(format!("integrity check failed for {name}: {result}"))
    }
}

/// A transactionally consistent copy of `src` at `dest` (`VACUUM INTO`),
/// safe while a writer holds `src` open (WAL included).
pub fn snapshot(src: &Path, dest: &Path) -> Result<(), String> {
    if let Some(dir) = dest.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let conn = read_only(src).map_err(|e| format!("open {}: {e}", src.display()))?;
    conn.execute("VACUUM INTO ?1", [dest.to_string_lossy().into_owned()])
        .map_err(|e| format!("snapshot {}: {e}", src.display()))?;
    Ok(())
}

/// `(id, path)` of every registered source, read without migrating.
pub fn registered_sources(control_db: &Path) -> Vec<(String, String)> {
    let Ok(conn) = read_only(control_db) else {
        return Vec::new();
    };
    let Ok(mut stmt) = conn.prepare("SELECT id, path FROM sources ORDER BY created_at, id") else {
        return Vec::new();
    };
    stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .map(|rows| rows.filter_map(Result::ok).collect())
        .unwrap_or_default()
}

/// `PRAGMA journal_mode` of an open connection.
pub fn journal_mode(conn: &Connection) -> String {
    conn.query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap_or_else(|_| "unknown".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn versions_snapshot_and_integrity() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("a.db");
        assert_eq!(schema_version(&db), None);
        let conn = Connection::open(&db).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode=WAL; CREATE TABLE t (x); INSERT INTO t VALUES (1);",
        )
        .unwrap();
        assert_eq!(schema_version(&db), Some(0));
        conn.execute_batch(
            "CREATE TABLE schema_migrations (version INTEGER, name TEXT, applied_at TEXT);
             INSERT INTO schema_migrations VALUES (3, 'x', 'now');",
        )
        .unwrap();
        assert_eq!(schema_version(&db), Some(3));
        let copy = dir.path().join("out/b.db");
        snapshot(&db, &copy).unwrap();
        assert_eq!(schema_version(&copy), Some(3));
        integrity_check(&copy).unwrap();
        std::fs::write(dir.path().join("bad.db"), b"not sqlite at all, sorry").unwrap();
        assert!(integrity_check(&dir.path().join("bad.db")).is_err());
        assert!(latest_knowledge_version() >= 6);
    }
}
