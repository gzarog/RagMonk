//! RagMonk application services.
//!
//! The operations the CLI, the Admin UI and the MCP server share, as plain
//! functions returning data: source registration ([`sources`]), indexing
//! runs ([`indexing`]), queries ([`query`]), status ([`status`]), AI
//! answers ([`ask`]) and the background daemon's lifecycle ([`daemon`]).
//! Rendering stays with each front end.

pub mod ask;
pub mod backend;
pub mod daemon;
pub mod indexing;
pub mod query;
pub mod server_index;
pub mod sources;
pub mod status;

use ragmonk_config::{load_config, LoadOptions, RagMonkConfig};
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;

/// The process's RagMonk home, with its directory layout created.
pub fn prepared_home() -> Result<Home, RagMonkError> {
    let home = Home::discover();
    home.ensure_layout().map_err(|e| {
        RagMonkError::new(
            ErrorKind::Generic,
            format!("cannot create {}: {e}", home.root().display()),
        )
    })?;
    Ok(home)
}

/// The layered configuration for `home`.
pub fn load(home: &Home) -> Result<RagMonkConfig, RagMonkError> {
    load_config(&LoadOptions {
        home: Some(home.root().to_path_buf()),
        ..LoadOptions::default()
    })
}

pub(crate) fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

pub(crate) fn db(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}
