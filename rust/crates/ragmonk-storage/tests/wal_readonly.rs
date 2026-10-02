//! Python homes use WAL. Opening one read-only must work and must not
//! change the database. SQLite may create empty `-wal`/`-shm` sidecars so
//! a concurrently running Python writer stays coordinated (`immutable=1`
//! would avoid them but could read torn pages); they must stay empty of
//! committed data.

use ragmonk_storage::db::open_read_only;

#[test]
fn read_only_open_of_wal_database_leaves_no_trace() {
    let tmp = tempfile::tempdir().unwrap();
    let db = tmp.path().join("sources.db");
    {
        let c = rusqlite::Connection::open(&db).unwrap();
        c.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE t(a); INSERT INTO t VALUES (1);")
            .unwrap();
    }
    let before: Vec<_> = std::fs::read_dir(tmp.path())
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    let bytes = std::fs::read(&db).unwrap();
    {
        let c = open_read_only(&db).unwrap();
        let n: i64 = c
            .query_row("SELECT COUNT(*) FROM t", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
        assert!(
            c.execute("INSERT INTO t VALUES (2)", []).is_err(),
            "must be read-only"
        );
    }
    assert_eq!(before.len(), 1);
    assert_eq!(
        std::fs::read(&db).unwrap(),
        bytes,
        "database bytes unchanged"
    );
    let wal = tmp.path().join("sources.db-wal");
    if wal.exists() {
        assert_eq!(
            std::fs::metadata(&wal).unwrap().len(),
            0,
            "read-only use writes no WAL frames"
        );
    }
}
