//! RagMonk V2 local storage (V3 clean-slate plan, phase RUST-02).
//!
//! Layout inside a RagMonk home:
//!
//! ```text
//! <home>/sources.db, <home>/projects/<id>/knowledge.db   Python V1 (read-only here)
//! <home>/v2/control.db                                   V2 control plane
//! <home>/v2/projects/<project_id>/knowledge.db           V2 per-source knowledge
//! ```
//!
//! V2 state is self-contained: nothing index-derived is imported from V1.
//! Source *definitions* (paths, include/exclude patterns, enabled flag) are
//! imported, and every imported or newly added source starts in
//! [`control::BuildState::NeedsFullRebuild`]. Python-era databases are only
//! ever opened read-only, so the Python reference keeps working until the
//! final cutover.

pub mod control;
pub mod db;
pub mod error;
pub mod knowledge;
pub mod migrate;
pub mod preflight;
pub mod schema;
pub mod search;
pub mod v1;
pub mod vectors;

pub use error::StorageError;

use std::path::{Path, PathBuf};

/// V2 paths under a RagMonk home.
#[derive(Debug, Clone)]
pub struct V2Layout {
    root: PathBuf,
    backups: PathBuf,
}

impl V2Layout {
    pub fn new(home: &ragmonk_core::paths::Home) -> Self {
        Self {
            root: home.root().join("v2"),
            backups: home.backups_dir().join("v2-migrations"),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn control_db(&self) -> PathBuf {
        self.root.join("control.db")
    }
    pub fn project_dir(&self, project_id: &str) -> PathBuf {
        self.root.join("projects").join(project_id)
    }
    pub fn project_db(&self, project_id: &str) -> PathBuf {
        self.project_dir(project_id).join("knowledge.db")
    }
    /// Where automatic pre-migration backups are written.
    pub fn migration_backups(&self) -> &Path {
        &self.backups
    }
}
