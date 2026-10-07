//! RagMonk core contracts shared by every other crate (plan phase RUST-01).
//!
//! Ported from the Python reference `ragmonk.core.{errors,models,paths}`,
//! `ragmonk.sources.registry` (source IDs), `ragmonk.backends.*_ids`
//! (server document IDs) and `ragmonk.security`.

pub mod errors;
pub mod ids;
pub mod models;
pub mod paths;
pub mod security;
pub mod version;

pub use errors::{ErrorKind, RagMonkError};
