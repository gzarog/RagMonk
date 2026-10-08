//! Full rebuilds of registered sources.

use std::path::Path;

use ragmonk_core::errors::RagMonkError;
use ragmonk_core::paths::Home;
use ragmonk_indexing::coordinator::{run_source, Options};
use ragmonk_service::load;
use ragmonk_service::sources::control_plane;
use ragmonk_storage::StorageLayout;
use serde_json::{json, Value};

use crate::{dberr, index_lock};

/// Rebuilds one or every enabled source under the `index` lock and
/// returns one `{id, path, scanned, indexed, failed, linked[, error]}` per
/// source; a source whose pass fails carries `error` and the run moves on.
/// `each` sees every outcome as it completes.
pub fn rebuild_sources(
    home: &Home,
    source_id: Option<&str>,
    fresh: bool,
    mut each: impl FnMut(&Value),
) -> Result<Vec<Value>, RagMonkError> {
    let cfg = load(home)?;
    let mut cp = control_plane(home)?;
    let sources = match source_id {
        Some(id) => vec![cp
            .get_source(id)
            .map_err(dberr)?
            .ok_or_else(|| RagMonkError::usage(format!("no such source: {id}")))?],
        None => cp.list_sources(true).map_err(dberr)?,
    };
    if sources.is_empty() {
        return Err(RagMonkError::usage("no sources to rebuild"));
    }
    if fresh {
        let unreachable: Vec<String> = sources
            .iter()
            .filter(|s| !Path::new(&s.path).exists())
            .map(|s| format!("{} ({})", s.id, s.path))
            .collect();
        if !unreachable.is_empty() {
            return Err(RagMonkError::usage(format!(
                "cannot rebuild --fresh: source root(s) not reachable: {}. Reconnect them (or remove the sources) and try again; the existing index was left untouched.",
                unreachable.join(", ")
            )));
        }
    }
    let lock = index_lock(home, "rebuild")?;
    let layout = StorageLayout::new(home);
    let registry =
        ragmonk_convert::registry_with(&cfg, &ragmonk_convert::RegistryOptions::for_home(home));
    let opts = Options::from_config(&cfg);
    let total = sources.len() as i64;
    let outcomes = ragmonk_indexing::progress::track(
        &home.index_progress(),
        "rebuild",
        Some(total),
        |tracker| -> Result<Vec<Value>, RagMonkError> {
            let mut out = Vec::new();
            for (i, s) in sources.iter().enumerate() {
                tracker.begin_source(&s.id, Some(i as i64 + 1));
                // One source failing must not stop the others: its error is
                // reported in its outcome and the run continues.
                let outcome = cp
                    .require_full_rebuild(&s.id, "manual rebuild")
                    .map_err(dberr)
                    .and_then(|()| run_source(&layout, &mut cp, s, &registry, &opts, tracker));
                let o = match outcome {
                    Ok(r) => json!({
                        "id": s.id,
                        "path": s.path,
                        "scanned": r.counts.scanned,
                        "indexed": r.indexed,
                        "failed": r.failed,
                        "linked": r.linked,
                    }),
                    Err(e) => json!({
                        "id": s.id,
                        "path": s.path,
                        "scanned": 0,
                        "indexed": 0,
                        "failed": 0,
                        "linked": 0,
                        "error": ragmonk_telemetry::redact::redact_urls_in_text(e.message()),
                    }),
                };
                each(&o);
                out.push(o);
            }
            Ok(out)
        },
    );
    lock.release();
    outcomes
}
