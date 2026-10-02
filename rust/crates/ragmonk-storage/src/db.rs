//! Connection factory and transactions, matching the reference's pragmas:
//! WAL, foreign keys on, `synchronous=NORMAL`, 5 s busy timeout, in-memory
//! temp store and a configurable page cache (MB).

use std::path::Path;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior};

use crate::error::{Result, StorageError};

pub const DEFAULT_CACHE_SIZE_MB: i64 = 64;
pub const BUSY_TIMEOUT: Duration = Duration::from_millis(5000);

/// Opens (creating if needed) a read-write database.
pub fn open(path: &Path, cache_size_mb: i64) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| StorageError::Io(format!("cannot create {}: {e}", parent.display())))?;
    }
    let ctx = format!("open {}", path.display());
    let conn = Connection::open(path).map_err(StorageError::sqlite(ctx.clone()))?;
    conn.busy_timeout(BUSY_TIMEOUT)
        .map_err(StorageError::sqlite(ctx.clone()))?;
    conn.execute_batch(&format!(
        "PRAGMA journal_mode = WAL;
         PRAGMA foreign_keys = ON;
         PRAGMA synchronous = NORMAL;
         PRAGMA temp_store = MEMORY;
         PRAGMA cache_size = -{};",
        cache_size_mb.max(1) * 1024
    ))
    .map_err(StorageError::sqlite(ctx))?;
    Ok(conn)
}

/// Opens an existing database strictly read-only (used for Python V1 data,
/// which this crate must never modify).
pub fn open_read_only(path: &Path) -> Result<Connection> {
    let ctx = format!("open read-only {}", path.display());
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(StorageError::sqlite(ctx.clone()))?;
    conn.busy_timeout(BUSY_TIMEOUT)
        .map_err(StorageError::sqlite(ctx.clone()))?;
    conn.execute_batch("PRAGMA query_only = ON;")
        .map_err(StorageError::sqlite(ctx))?;
    Ok(conn)
}

/// `BEGIN IMMEDIATE` transaction: commits on `Ok`, rolls back on `Err`.
/// Callers must not hold it across ML inference, document conversion or
/// network I/O.
pub fn write_tx<T>(
    conn: &mut Connection,
    f: impl FnOnce(&Transaction<'_>) -> Result<T>,
) -> Result<T> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(StorageError::sqlite("begin transaction"))?;
    let out = f(&tx)?;
    tx.commit().map_err(StorageError::sqlite("commit"))?;
    Ok(out)
}

pub fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
