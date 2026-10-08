//! The status model: sources, builds, progress, locks, queue and errors.

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use serde_json::json;
use serde_json::Value;

use crate::load;
use crate::sources::control_plane;

/// The full status model (`status_service.collect_status`).
pub fn collect(home: &Home) -> Result<Value, RagMonkError> {
    let cfg = load(home)?;
    let cp = control_plane(home)?;
    let mut data = ragmonk_indexing::status::collect_status(
        home,
        &cp,
        cfg.indexing.status_stall_threshold_seconds,
        chrono::Utc::now(),
    )
    .map_err(|e| RagMonkError::new(ErrorKind::Database, e.to_string()))?;
    use ragmonk_documents::tokenizer as t;
    data["tokenizer"] = json!({
        "model_id": t::EMBEDDING_MODEL_ID,
        "revision": t::TOKENIZER_REVISION,
        "fingerprint": t::tokenizer_fingerprint(),
        "max_sequence_tokens": t::MAX_SEQUENCE_TOKENS,
        "chunk_ceiling": cfg.documents.chunking.resolved_max_tokens(),
    });
    if cfg.storage.mode == "server" {
        // Server-side counts need a server read path that does not exist yet.
        // Never fabricate them.
        data["backend"] = json!({
            "type": cfg.storage.server.engine.as_str(),
            "error": "server-mode status counts are not available yet",
        });
    }
    Ok(data)
}
