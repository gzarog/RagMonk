//! RagMonk local storage.
//!
//! Layout inside a RagMonk home:
//!
//! ```text
//! <home>/v2/control.db                                   control plane
//! <home>/v2/projects/<project_id>/knowledge.db           per-source knowledge
//! ```
//!
//! Every new source starts in [`control::BuildState::NeedsFullRebuild`].

pub mod control;
pub mod db;
pub mod error;
pub mod knowledge;
pub mod maintenance;
pub mod migrate;
pub mod schema;
pub mod search;
pub mod status;
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
