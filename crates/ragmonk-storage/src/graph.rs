//! The relationship graph's lifecycle in one project store.
//!
//! The graph (code relationships and code-document links) is derived after
//! the base index is published and is versioned independently of it. Its
//! state lives in the knowledge database's `metadata` table under
//! [`GRAPH_STATE_KEY`], so no schema change is needed:
//!
//! * `base_build_id` + `base_generation` name the exact published base the
//!   graph rows were derived from. A base generation is the build id plus
//!   the build's `finished_at`, which changes on every publication (full
//!   or in-place incremental).
//! * Graph readers ([`ProjectStore::graph_visible`]) only return graph rows
//!   when the state is `ready` **and** its base generation equals the
//!   active base's current generation. Anything else (missing, building,
//!   failed, disabled, stale) is reported as an explicit status and graph
//!   queries come back empty; regular search is unaffected.
//! * Graph rows are written and the state is promoted to `ready` in one
//!   SQLite transaction, so a failed or crashed graph build is never
//!   visible and never touches base rows.

use std::collections::BTreeMap;

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::db::{now_iso, write_tx};
use crate::error::{Result, StorageError};
use crate::knowledge::{cached, ProjectStore, RelationshipRow};

/// `metadata` key holding the JSON [`GraphState`] (small: status reads it).
pub const GRAPH_STATE_KEY: &str = "graph_state";
/// `metadata` key holding the graph's per-file dependency keys (JSON
/// `file_id -> key`), read only by the graph stage.
pub const GRAPH_FILES_KEY: &str = "graph_files";

/// Graph lifecycle.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum GraphLifecycle {
    /// `indexing.relationships_enabled: false`: no graph jobs; any retained
    /// graph rows are hidden.
    Disabled,
    /// No graph was built for this source yet.
    #[default]
    Pending,
    Building,
    Ready,
    /// The graph matches an older base generation (or its inputs changed).
    Stale,
    Failed,
}

impl GraphLifecycle {
    pub fn as_str(self) -> &'static str {
        match self {
            GraphLifecycle::Disabled => "disabled",
            GraphLifecycle::Pending => "pending",
            GraphLifecycle::Building => "building",
            GraphLifecycle::Ready => "ready",
            GraphLifecycle::Stale => "stale",
            GraphLifecycle::Failed => "failed",
        }
    }
}

/// The recorded graph state of one source.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GraphState {
    pub state: GraphLifecycle,
    /// Monotonic graph generation; incremented by every promotion.
    #[serde(default)]
    pub generation: u64,
    /// Base build the committed graph rows belong to.
    #[serde(default)]
    pub base_build_id: Option<String>,
    /// Base generation the committed graph rows were derived from.
    #[serde(default)]
    pub base_generation: Option<String>,
    /// Graph derivation identity (code parser + graph rules).
    #[serde(default)]
    pub derivation_version: Option<String>,
    /// Dependency metadata: `file_id -> file key` (content hash and the
    /// time the base last wrote the file) each file's graph rows were
    /// derived from. Stored under [`GRAPH_FILES_KEY`], loaded only by
    /// [`ProjectStore::graph_state_with_files`]. Empty means "recompute
    /// everything".
    #[serde(skip)]
    pub files: BTreeMap<String, String>,
    #[serde(default)]
    pub last_success_at: Option<String>,
    #[serde(default)]
    pub last_error: Option<String>,
    #[serde(default)]
    pub stale_reason: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

/// A graph state as reported (resolved against the active base).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GraphStatus {
    /// Effective state: a `ready` graph of an older base reports `stale`.
    pub state: GraphLifecycle,
    pub generation: u64,
    pub base_build_id: Option<String>,
    pub base_generation: Option<String>,
    pub last_success_at: Option<String>,
    pub last_error: Option<String>,
    pub stale_reason: Option<String>,
}

impl GraphState {
    /// The effective status given the active base generation.
    pub fn status(&self, active_generation: Option<&str>) -> GraphStatus {
        let mut state = self.state;
        let mut stale_reason = self.stale_reason.clone();
        if state == GraphLifecycle::Ready && self.base_generation.as_deref() != active_generation {
            state = GraphLifecycle::Stale;
            stale_reason.get_or_insert_with(|| "the base index was republished".into());
        }
        GraphStatus {
            state,
            generation: self.generation,
            base_build_id: self.base_build_id.clone(),
            base_generation: self.base_generation.clone(),
            last_success_at: self.last_success_at.clone(),
            last_error: self.last_error.clone(),
            stale_reason,
        }
    }
}

/// `file_id -> key` of a build's files, the per-file graph dependency key.
pub type FileKeys = BTreeMap<String, String>;

/// One published file as the graph stage sees it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphFile {
    pub id: String,
    pub rel_path: String,
    pub kind: String,
    pub status: String,
    pub content_hash: Option<String>,
    /// Content hash plus the time the base last wrote the file's rows.
    pub key: String,
}

fn invalid(e: impl std::fmt::Display) -> StorageError {
    StorageError::Invalid(e.to_string())
}

impl ProjectStore {
    /// The recorded graph state (default `pending` when none was recorded
    /// or the record is unreadable).
    pub fn graph_state(&self) -> Result<GraphState> {
        let raw: Option<String> = self
            .conn
            .query_row(
                "SELECT value FROM metadata WHERE key = ?1",
                [GRAPH_STATE_KEY],
                |r| r.get(0),
            )
            .optional()
            .map_err(StorageError::sqlite("read graph state"))?;
        Ok(raw
            .and_then(|r| serde_json::from_str(&r).ok())
            .unwrap_or_default())
    }

    /// [`Self::graph_state`] with its per-file dependency keys.
    pub fn graph_state_with_files(&self) -> Result<GraphState> {
        let mut state = self.graph_state()?;
        let raw: Option<String> = self
            .conn
            .query_row(
                "SELECT value FROM metadata WHERE key = ?1",
                [GRAPH_FILES_KEY],
                |r| r.get(0),
            )
            .optional()
            .map_err(StorageError::sqlite("read graph files"))?;
        state.files = raw
            .and_then(|r| serde_json::from_str(&r).ok())
            .unwrap_or_default();
        Ok(state)
    }

    fn put_metadata(&mut self, key: &str, value: &str) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            cached(
                tx,
                "INSERT INTO metadata (key, value) VALUES (?1, ?2)
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                params![key, value],
            )
            .map_err(StorageError::sqlite("write graph state"))?;
            Ok(())
        })
    }

    /// Stores the graph state (inside the caller's session when one is
    /// open). The per-file dependency keys are left as they are.
    pub fn put_graph_state(&mut self, state: &GraphState) -> Result<()> {
        let mut state = state.clone();
        state.updated_at = Some(now_iso());
        let value = serde_json::to_string(&state).map_err(invalid)?;
        self.put_metadata(GRAPH_STATE_KEY, &value)
    }

    /// Stores the state together with its per-file dependency keys (a
    /// promotion; inside the graph transaction).
    pub fn put_graph_state_with_files(&mut self, state: &GraphState) -> Result<()> {
        let files = serde_json::to_string(&state.files).map_err(invalid)?;
        self.put_metadata(GRAPH_FILES_KEY, &files)?;
        self.put_graph_state(state)
    }

    /// Updates only the lifecycle fields of the stored state, keeping the
    /// committed generation and dependency metadata.
    pub fn set_graph_lifecycle(
        &mut self,
        state: GraphLifecycle,
        error: Option<&str>,
        stale_reason: Option<&str>,
    ) -> Result<GraphState> {
        let mut s = self.graph_state()?;
        s.state = state;
        if error.is_some() || state != GraphLifecycle::Failed {
            s.last_error = error.map(str::to_owned);
        }
        s.stale_reason = stale_reason.map(str::to_owned);
        self.put_graph_state(&s)?;
        Ok(s)
    }

    /// The current generation of a published base build:
    /// `<build_id>@<finished_at>`. `None` when the build is not published.
    pub fn base_generation(&self, build_id: &str) -> Result<Option<String>> {
        let finished: Option<Option<String>> = self
            .conn
            .query_row(
                "SELECT finished_at FROM builds WHERE id = ?1 AND status = 'published'",
                [build_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(StorageError::sqlite("base generation"))?;
        Ok(finished.map(|f| format!("{build_id}@{}", f.unwrap_or_default())))
    }

    /// Whether graph rows of `build_id` may be returned to readers: the
    /// graph is `ready` and was derived from the build's current
    /// generation.
    pub fn graph_visible(&self, build_id: &str) -> Result<bool> {
        let state = self.graph_state()?;
        if state.state != GraphLifecycle::Ready {
            return Ok(false);
        }
        Ok(state.base_generation.is_some()
            && state.base_generation == self.base_generation(build_id)?)
    }

    /// The effective graph status against `build_id` (the active base).
    pub fn graph_status(&self, build_id: Option<&str>) -> Result<GraphStatus> {
        let generation = match build_id {
            Some(b) => self.base_generation(b)?,
            None => None,
        };
        Ok(self.graph_state()?.status(generation.as_deref()))
    }

    /// Every file of a published build with its graph dependency key.
    pub fn graph_files(&self, build_id: &str) -> Result<Vec<GraphFile>> {
        self.query_rows(
            "graph files",
            "SELECT id, rel_path, kind, status, content_hash, last_indexed_at
             FROM files WHERE build_id = ?1 ORDER BY id",
            &[&build_id],
            |r| {
                let hash: Option<String> = r.get(4)?;
                let at: Option<String> = r.get(5)?;
                Ok(GraphFile {
                    id: r.get(0)?,
                    rel_path: r.get(1)?,
                    kind: r.get(2)?,
                    status: r.get(3)?,
                    key: format!(
                        "{}@{}",
                        hash.as_deref().unwrap_or(""),
                        at.as_deref().unwrap_or("")
                    ),
                    content_hash: hash,
                })
            },
        )
    }

    /// Deletes every graph row (relationships and automatic/manual link
    /// projections) of `build_id`. Base rows are never touched.
    pub fn clear_graph(&mut self, build_id: &str) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            for sql in [
                "DELETE FROM relationships WHERE build_id = ?1",
                "DELETE FROM cross_links WHERE build_id = ?1",
            ] {
                cached(tx, sql, [build_id]).map_err(StorageError::sqlite("clear graph"))?;
            }
            Ok(())
        })
    }

    /// Deletes the graph rows derived from (or pointing into) one file.
    pub fn clear_file_graph(&mut self, build_id: &str, file_id: &str) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            for sql in [
                "DELETE FROM relationships WHERE build_id = ?1 AND file_id = ?2",
                "DELETE FROM cross_links WHERE build_id = ?1 AND document_id IN
                    (SELECT id FROM documents WHERE build_id = ?1 AND file_id = ?2)",
                "DELETE FROM cross_links WHERE build_id = ?1 AND entity_id IN
                    (SELECT id FROM entities WHERE build_id = ?1 AND file_id = ?2)",
            ] {
                cached(tx, sql, params![build_id, file_id])
                    .map_err(StorageError::sqlite("clear file graph"))?;
            }
            Ok(())
        })
    }

    /// Inserts relationship rows into `build_id`.
    pub fn put_relationships(&mut self, build_id: &str, rows: &[RelationshipRow]) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            for r in rows {
                cached(
                    tx,
                    "INSERT OR REPLACE INTO relationships (id, build_id, file_id, relationship_type,
                        source_entity_id, target_entity_id, target_symbol, resolver, confidence,
                        source_location, evidence, reference_text)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
                    params![
                        r.id,
                        build_id,
                        r.file_id,
                        r.relationship_type,
                        r.source_entity_id,
                        r.target_entity_id,
                        r.target_symbol,
                        r.resolver,
                        r.confidence,
                        r.source_location,
                        r.evidence,
                        r.reference_text
                    ],
                )
                .map_err(StorageError::sqlite("insert relationship"))?;
            }
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_graph_of_an_older_base_reports_stale() {
        let s = GraphState {
            state: GraphLifecycle::Ready,
            base_generation: Some("b@1".into()),
            ..GraphState::default()
        };
        assert_eq!(s.status(Some("b@1")).state, GraphLifecycle::Ready);
        let st = s.status(Some("b@2"));
        assert_eq!(st.state, GraphLifecycle::Stale);
        assert!(st.stale_reason.is_some());
        assert_eq!(s.status(None).state, GraphLifecycle::Stale);
    }

    #[test]
    fn file_keys_are_not_part_of_the_state_record() {
        let s = GraphState {
            files: [("f".to_owned(), "k".to_owned())].into(),
            ..GraphState::default()
        };
        assert!(!serde_json::to_string(&s).unwrap().contains("\"f\""));
    }

    #[test]
    fn unknown_fields_default() {
        let s: GraphState = serde_json::from_str(r#"{"state":"failed"}"#).unwrap();
        assert_eq!(s.state, GraphLifecycle::Failed);
        assert!(s.files.is_empty());
    }
}
