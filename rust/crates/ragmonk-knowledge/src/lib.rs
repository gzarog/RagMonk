//! Knowledge linker (RUST-08): cross-domain links between code entities and
//! document chunks, and explicit (manual) links.

pub mod linker;
pub mod manual;
pub mod matcher;

use ragmonk_indexing::coordinator::{BuildFinalizer, ProcessError};
use ragmonk_storage::knowledge::ProjectStore;

/// Links the files written in a build, then re-applies manual links.
pub struct KnowledgeLinker;

impl BuildFinalizer for KnowledgeLinker {
    fn finalize(
        &self,
        store: &mut ProjectStore,
        build_id: &str,
        touched: &[String],
    ) -> Result<(), ProcessError> {
        let err = |e: ragmonk_storage::StorageError| ProcessError {
            code: "link_error".into(),
            message: e.to_string(),
            transient: false,
        };
        let started = std::time::Instant::now();
        let candidates = linker::link_touched_files(store, build_id, touched).map_err(err)?;
        let rows: Vec<_> = candidates.iter().map(linker::Candidate::row).collect();
        for batch in rows.chunks(linker::LINK_BATCH_SIZE) {
            ragmonk_indexing::progress::heartbeat();
            store.put_links(build_id, batch).map_err(err)?;
        }
        let manual = manual::apply(store, build_id).map_err(err)?;
        tracing::info!(
            component = "linker",
            event = "links_published",
            links = rows.len(),
            manual,
            seconds = started.elapsed().as_secs_f64()
        );
        Ok(())
    }
}
