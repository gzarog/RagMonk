//! RagMonk operations shared by the CLI and the Admin UI: health checks
//! ([`doctor`]), backup and restore ([`backup`]), full rebuilds
//! ([`rebuild`]), vector maintenance ([`vectors`]) and data removal
//! ([`uninstall`]).

pub mod backup;
pub mod doctor;
pub mod rebuild;
pub mod uninstall;
pub mod vectors;

use std::time::Duration;

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_indexing::lock::RunLock;
use ragmonk_service::load;

pub(crate) fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

pub(crate) fn dberr(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

pub(crate) fn index_lock(home: &Home, operation: &str) -> Result<RunLock, RagMonkError> {
    let cfg = load(home)?;
    RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        operation,
        None,
        Duration::from_secs_f64(cfg.indexing.lock_timeout_seconds),
    )
}

pub(crate) fn now_iso() -> String {
    ragmonk_indexing::progress::now_iso()
}
