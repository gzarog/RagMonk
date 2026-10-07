//! Data removal for `uninstall`.

use ragmonk_core::errors::RagMonkError;
use ragmonk_core::paths::Home;
use ragmonk_indexing::daemon::pid;

use crate::backup::STOP_TIMEOUT;
use crate::generic;

/// Deletes the whole RagMonk home, stopping a running daemon first.
/// Returns whether anything was deleted.
pub fn purge_data(home: &Home) -> Result<bool, RagMonkError> {
    if !home.root().exists() {
        return Ok(false);
    }
    pid::stop_and_wait(home, STOP_TIMEOUT, "delete this data").map_err(generic)?;
    std::fs::remove_dir_all(home.root()).map_err(generic)?;
    Ok(true)
}
