//! RagMonk status: the single status model and its producers.
//!
//! * [`model`]: the canonical status contract every surface renders.
//!
//! Nothing else in the workspace builds a status report: the CLI, MCP
//! server and Admin UI all consume [`model::StatusReport`].

pub mod model;

pub use model::{CollectError, StatusReport, SCHEMA_VERSION};
