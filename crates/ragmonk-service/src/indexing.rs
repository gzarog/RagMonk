//! Indexing runs over registered sources.
//!
//! Each source is processed under the `index` lock on its own: a source
//! that cannot be locked, fails, or is unreachable is reported and the run
//! moves on to the next one. Progress is published to the home's progress
//! file while the run lasts.

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_indexing::coordinator::{run_source, Options, SourceResult};
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::control::SourceRecord;
use ragmonk_storage::StorageLayout;

use crate::sources::{control_plane, get_source};
use crate::{db, load};

/// What happened to one source during a run.
pub enum SourceEvent<'a> {
    /// The source is next; its lock has not been taken yet.
    Started { source: &'a SourceRecord },
    /// Another process holds the `index` lock for it.
    Blocked {
        source: &'a SourceRecord,
        error: &'a RagMonkError,
    },
    /// The pass failed before completing.
    Failed {
        source: &'a SourceRecord,
        error: &'a RagMonkError,
    },
    /// The pass ran; `run` says what it did (including offline/incomplete).
    Completed {
        source: &'a SourceRecord,
        run: &'a SourceResult,
    },
}

/// The outcome of a whole run.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct RunSummary {
    pub attempted: usize,
    pub failed_sources: usize,
    pub failed_files: usize,
}

impl RunSummary {
    /// An `IndexingPartialFailure` error when anything failed.
    pub fn into_result(self) -> Result<Self, RagMonkError> {
        if self.failed_sources == 0 && self.failed_files == 0 {
            return Ok(self);
        }
        let mut parts = Vec::new();
        if self.failed_sources > 0 {
            parts.push(format!("{} source(s) failed", self.failed_sources));
        }
        if self.failed_files > 0 {
            parts.push(format!("{} file(s) failed to index", self.failed_files));
        }
        Err(RagMonkError::new(
            ErrorKind::IndexingPartialFailure,
            format!("{}; see 'ragmonk doctor' for details", parts.join("; ")),
        ))
    }
}

/// The sources a run covers: one by id, or every enabled source.
pub fn selected_sources(
    home: &Home,
    source_id: Option<&str>,
) -> Result<Vec<SourceRecord>, RagMonkError> {
    let cp = control_plane(home)?;
    match source_id {
        Some(id) => Ok(vec![get_source(&cp, id)?]),
        None => cp.list_sources(true).map_err(db),
    }
}

/// Indexes `sources` in order, reporting each through `on_event`.
pub fn index_sources(
    home: &Home,
    sources: &[SourceRecord],
    operation: &str,
    mut on_event: impl FnMut(SourceEvent<'_>),
) -> Result<RunSummary, RagMonkError> {
    let cfg = load(home)?;
    let mut cp = control_plane(home)?;
    let layout = StorageLayout::new(home);
    let registry =
        ragmonk_convert::registry_with(&cfg, &ragmonk_convert::RegistryOptions::for_home(home));
    let opts = Options::from_config(&cfg);
    let mut summary = RunSummary {
        attempted: sources.len(),
        ..RunSummary::default()
    };
    let total = sources.len() as i64;
    ragmonk_indexing::progress::track(&home.index_progress(), operation, Some(total), |tracker| {
        for (i, source) in sources.iter().enumerate() {
            tracker.begin_source(&source.id, Some(i as i64 + 1));
            on_event(SourceEvent::Started { source });
            let lock = match RunLock::acquire(
                &home.locks_dir().join("index.lock"),
                operation,
                Some(&source.id),
                opts.lock_timeout,
            ) {
                Ok(l) => l,
                Err(error) => {
                    summary.failed_sources += 1;
                    on_event(SourceEvent::Blocked {
                        source,
                        error: &error,
                    });
                    continue;
                }
            };
            let outcome = run_source(&layout, &mut cp, source, &registry, &opts, tracker);
            lock.release();
            match outcome {
                Ok(run) => {
                    if run.offline.is_none() {
                        summary.failed_files += run.failed;
                    }
                    on_event(SourceEvent::Completed { source, run: &run });
                }
                Err(error) => {
                    summary.failed_sources += 1;
                    on_event(SourceEvent::Failed {
                        source,
                        error: &error,
                    });
                }
            }
        }
        Ok::<(), RagMonkError>(())
    })?;
    Ok(summary)
}
