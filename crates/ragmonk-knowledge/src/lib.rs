//! Knowledge linker: cross-domain links between code entities and
//! document chunks, and explicit (manual) links.

pub mod linker;
pub mod manual;
pub mod matcher;

use ragmonk_code::graph_stage::GraphLinker;
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::StorageError;

/// The graph stage's linker: links the touched files, then re-applies
/// manual link intent. It never runs while the base index is built.
pub struct KnowledgeLinker;

impl GraphLinker for KnowledgeLinker {
    fn link(
        &self,
        store: &mut ProjectStore,
        build_id: &str,
        touched: &[String],
        _full: bool,
    ) -> Result<usize, StorageError> {
        let started = std::time::Instant::now();
        let candidates = linker::link_touched_files(store, build_id, touched)?;
        let rows: Vec<_> = candidates.iter().map(linker::Candidate::row).collect();
        for batch in rows.chunks(linker::LINK_BATCH_SIZE) {
            ragmonk_indexing::progress::heartbeat();
            store.put_links(build_id, batch)?;
        }
        let manual = manual::apply(store, build_id)?;
        tracing::info!(
            component = "linker",
            event = "links_published",
            links = rows.len(),
            manual,
            seconds = started.elapsed().as_secs_f64()
        );
        Ok(rows.len())
    }
}

/// Builds (or confirms) the relationship graph of a source's published
/// `build_id`: code relationships, cross-file resolution, code-document
/// links and manual link projection (see [`ragmonk_code::graph_stage`]).
pub fn build_graph(
    store: &mut ProjectStore,
    root: &std::path::Path,
    build_id: &str,
    workers: usize,
    cancel: Option<&std::sync::atomic::AtomicBool>,
    progress: &mut ragmonk_code::graph_stage::GraphProgressFn<'_>,
) -> Result<ragmonk_code::graph_stage::GraphReport, ragmonk_code::graph_stage::GraphError> {
    ragmonk_code::graph_stage::build_graph(
        store,
        root,
        build_id,
        &ragmonk_code::graph_stage::GraphOptions {
            workers,
            linker: Some(&KnowledgeLinker),
            cancel,
        },
        progress,
    )
}
