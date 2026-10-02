//! Read-only schema/row-count inventory of RagMonk SQLite databases.
//!
//! Opened with `SQLITE_OPEN_READ_ONLY` so capturing a baseline can never
//! migrate, vacuum or otherwise mutate user-shaped data.

use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};

/// Inventories every `*.db` file under `root`, keyed by its path relative
/// to `root` (with `/` separators). `rename` maps path segments (e.g.
/// path-derived project directory names) to placeholders.
pub fn inventory(root: &Path, rename: &dyn Fn(&str) -> String) -> anyhow::Result<Value> {
    let mut files = Vec::new();
    collect_dbs(root, &mut files)?;
    files.sort();
    let mut out = Map::new();
    for file in files {
        let rel = file
            .strip_prefix(root)
            .unwrap_or(&file)
            .components()
            .map(|c| rename(&c.as_os_str().to_string_lossy()))
            .collect::<Vec<_>>()
            .join("/");
        out.insert(rel, inventory_db(&file)?);
    }
    Ok(Value::Object(out))
}

fn collect_dbs(dir: &Path, out: &mut Vec<PathBuf>) -> anyhow::Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let kind = entry.file_type()?;
        if kind.is_dir() {
            collect_dbs(&path, out)?;
        } else if kind.is_file() && path.extension().is_some_and(|e| e == "db") {
            out.push(path);
        }
    }
    Ok(())
}

pub fn inventory_db(path: &Path) -> anyhow::Result<Value> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    let user_version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    let mut stmt = conn.prepare(
        "SELECT type, name, tbl_name FROM sqlite_master \
         WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name",
    )?;
    let objects: Vec<(String, String, String)> = stmt
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .collect::<Result<_, _>>()?;

    let mut tables = Map::new();
    let mut indexes = Vec::new();
    let mut triggers = Vec::new();
    let mut views = Vec::new();
    for (kind, name, table) in objects {
        match kind.as_str() {
            "table" => {
                let columns = table_columns(&conn, &name)?;
                let quoted = name.replace('"', "\"\"");
                let rows: i64 =
                    conn.query_row(&format!("SELECT COUNT(*) FROM \"{quoted}\""), [], |r| {
                        r.get(0)
                    })?;
                tables.insert(name, json!({"columns": columns, "rows": rows}));
            }
            "index" => indexes.push(json!({"name": name, "table": table})),
            "trigger" => triggers.push(json!({"name": name, "table": table})),
            "view" => views.push(Value::String(name)),
            _ => {}
        }
    }
    Ok(json!({
        "user_version": user_version,
        "tables": tables,
        "indexes": indexes,
        "triggers": triggers,
        "views": views,
    }))
}

fn table_columns(conn: &Connection, table: &str) -> anyhow::Result<Vec<Value>> {
    let mut stmt = conn.prepare("SELECT name, type, \"notnull\", pk FROM pragma_table_info(?1)")?;
    let cols = stmt
        .query_map([table], |r| {
            Ok(json!({
                "name": r.get::<_, String>(0)?,
                "type": r.get::<_, String>(1)?,
                "notnull": r.get::<_, i64>(2)? != 0,
                "pk": r.get::<_, i64>(3)?,
            }))
        })?
        .collect::<Result<_, _>>()?;
    Ok(cols)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inventories_schema_and_counts_read_only() {
        let dir = tempfile::tempdir().unwrap();
        let sub = dir.path().join("projects").join("abc");
        std::fs::create_dir_all(&sub).unwrap();
        let db = sub.join("knowledge.db");
        {
            let conn = Connection::open(&db).unwrap();
            conn.execute_batch(
                "PRAGMA user_version=7; CREATE TABLE t(id TEXT PRIMARY KEY, n INTEGER NOT NULL);
                 CREATE INDEX t_n ON t(n); INSERT INTO t VALUES('a',1),('b',2);",
            )
            .unwrap();
        }
        let before = std::fs::read(&db).unwrap();
        let rename = |s: &str| {
            if s == "abc" {
                "<PROJECT>".into()
            } else {
                s.into()
            }
        };
        let inv = inventory(dir.path(), &rename).unwrap();
        let entry = &inv["projects/<PROJECT>/knowledge.db"];
        assert_eq!(entry["user_version"], 7);
        assert_eq!(entry["tables"]["t"]["rows"], 2);
        assert_eq!(entry["tables"]["t"]["columns"][1]["notnull"], true);
        assert_eq!(entry["indexes"][0]["name"], "t_n");
        assert_eq!(
            std::fs::read(&db).unwrap(),
            before,
            "inventory must not write"
        );
    }
}
