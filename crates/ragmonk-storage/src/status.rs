//! Read-only status queries over one project store: file counts
//! by status, retry/failure queue stats and recent errors.

use std::collections::BTreeMap;

use crate::error::Result;
use crate::knowledge::ProjectStore;

/// One recorded indexing error.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct ErrorRecord {
    pub path: Option<String>,
    pub error_code: String,
    pub error_message: String,
    pub occurred_at: String,
}

/// A file's `(last_indexed_at, updated_at)`.
pub type FileTimes = (Option<String>, Option<String>);

/// Retry/failure state of a build's files: the job-queue stats, kept as
/// retry state on file rows.
#[derive(Debug, Clone, Default, PartialEq, Eq, serde::Serialize)]
pub struct RetryStats {
    pub retry: i64,
    pub failed: i64,
    pub completed: i64,
    pub next_retry_at: Option<String>,
    pub max_attempt_count: i64,
}

impl ProjectStore {
    /// File counts per status in `build_id`.
    pub fn file_counts_by_status(&self, build_id: &str) -> Result<BTreeMap<String, i64>> {
        Ok(self
            .query_rows(
                "file counts",
                "SELECT status, COUNT(*) FROM files WHERE build_id = ?1 GROUP BY status",
                &[&build_id],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            )?
            .into_iter()
            .collect())
    }

    pub fn retry_stats(&self, build_id: &str) -> Result<RetryStats> {
        Ok(self
            .query_rows(
                "retry stats",
                "SELECT COALESCE(SUM(status = 'retry'), 0), COALESCE(SUM(status = 'failed'), 0),
                        COALESCE(SUM(status = 'indexed'), 0),
                        MIN(CASE WHEN status = 'retry' THEN next_attempt_at END),
                        COALESCE(MAX(attempt_count), 0)
                 FROM files WHERE build_id = ?1",
                &[&build_id],
                |r| {
                    Ok(RetryStats {
                        retry: r.get(0)?,
                        failed: r.get(1)?,
                        completed: r.get(2)?,
                        next_retry_at: r.get(3)?,
                        max_attempt_count: r.get(4)?,
                    })
                },
            )?
            .pop()
            .unwrap_or_default())
    }

    /// The latest error of a file currently waiting for a retry.
    pub fn latest_retry_error(&self, build_id: &str) -> Result<Option<ErrorRecord>> {
        Ok(self
            .query_rows(
                "latest retry error",
                "SELECT e.rel_path, e.error_code, e.error_message, e.occurred_at
                 FROM index_errors e
                 JOIN files f ON f.build_id = ?1 AND f.id = e.file_id AND f.status = 'retry'
                 ORDER BY e.occurred_at DESC, e.id DESC LIMIT 1",
                &[&build_id],
                error_row,
            )?
            .pop())
    }

    /// `(last_indexed_at, updated_at)` of every file in `build_id`.
    pub fn file_times(&self, build_id: &str) -> Result<BTreeMap<String, FileTimes>> {
        Ok(self
            .query_rows(
                "file times",
                "SELECT id, last_indexed_at, updated_at FROM files WHERE build_id = ?1",
                &[&build_id],
                |r| Ok((r.get::<_, String>(0)?, (r.get(1)?, r.get(2)?))),
            )?
            .into_iter()
            .collect())
    }

    /// Most recent errors of this source, newest first.
    pub fn recent_errors(&self, limit: i64) -> Result<Vec<ErrorRecord>> {
        let source = self.source_id().to_owned();
        self.query_rows(
            "recent errors",
            "SELECT rel_path, error_code, error_message, occurred_at FROM index_errors
             WHERE source_id = ?1 ORDER BY occurred_at DESC, id DESC LIMIT ?2",
            &[&source, &limit],
            error_row,
        )
    }
}

fn error_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<ErrorRecord> {
    Ok(ErrorRecord {
        path: r.get(0)?,
        error_code: r.get(1)?,
        error_message: r.get(2)?,
        occurred_at: r.get(3)?,
    })
}
