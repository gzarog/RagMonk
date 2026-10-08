//! The status model: sources, builds, progress, locks, queue and errors.
//!
//! In server mode every source, build, count, retry and error comes from
//! the server (the published build of each source); only this host's
//! run progress and lock files are read locally.

use std::collections::BTreeMap;

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_indexing::status as st;
use serde_json::json;
use serde_json::{Map, Value};

use crate::backend::{server_err, Backend};
use crate::load;
use crate::sources::{control_plane, millis_to_iso, Catalog};

/// Recent errors kept in the payload.
const RECENT_ERRORS: usize = 25;

/// The full status model (`status_service.collect_status`).
pub fn collect(home: &Home) -> Result<Value, RagMonkError> {
    let cfg = load(home)?;
    let mut data = match crate::backend::open(home)? {
        Backend::Local => {
            let cp = control_plane(home)?;
            ragmonk_indexing::status::collect_status(
                home,
                &cp,
                cfg.indexing.status_stall_threshold_seconds,
                chrono::Utc::now(),
            )
            .map_err(|e| RagMonkError::new(ErrorKind::Database, e.to_string()))?
        }
        Backend::Server(server) => collect_server(home, &cfg, server)?,
    };
    use ragmonk_documents::tokenizer as t;
    data["tokenizer"] = json!({
        "model_id": t::EMBEDDING_MODEL_ID,
        "revision": t::TOKENIZER_REVISION,
        "fingerprint": t::tokenizer_fingerprint(),
        "max_sequence_tokens": t::MAX_SEQUENCE_TOKENS,
        "chunk_ceiling": cfg.documents.chunking.resolved_max_tokens(),
    });
    Ok(data)
}

fn collect_server(
    home: &Home,
    cfg: &ragmonk_config::RagMonkConfig,
    server: std::sync::Arc<ragmonk_backends::ServerBackend>,
) -> Result<Value, RagMonkError> {
    let indexer = st::indexer_state(
        home,
        cfg.indexing.status_stall_threshold_seconds,
        chrono::Utc::now(),
    );
    let catalog = Catalog::Server(server.clone());
    let health = server.cluster_health().map_err(server_err)?;
    let mut rows = Vec::new();
    let mut errors = Vec::new();
    let mut builds = Vec::new();
    for source in catalog.list(false)? {
        let state = server.source_state(&source.id).map_err(server_err)?;
        let f = |k: &str| state.as_ref().and_then(|s| s.field(k)).map(str::to_owned);
        let active = f("active_build_id");
        let mut counts = Map::new();
        let mut queue = json!({
            "queued": 0, "processing": 0, "retry": 0, "failed": 0, "completed": 0, "depth": 0,
            "oldest_pending_created_at": null, "oldest_processing_started_at": null,
            "next_retry_at": null, "max_attempt_count": 0, "latest_job_error": null,
        });
        let mut metrics = json!({
            "symbols_created": 0, "relationships_created": 0, "documents_processed": 0,
            "chunks": 0, "links": 0, "database_size_bytes": 0,
        });
        let mut last_error_detail = Value::Null;
        if let Some(build) = &active {
            let stats = server
                .source_stats(&source.id, build, RECENT_ERRORS)
                .map_err(server_err)?;
            for (k, v) in &stats.files_by_status {
                counts.insert(k.clone(), json!(v));
            }
            let n = |k: &str| stats.files_by_status.get(k).copied().unwrap_or(0);
            queue["retry"] = json!(n("retry"));
            queue["failed"] = json!(n("failed"));
            queue["completed"] = json!(n("indexed"));
            queue["depth"] = json!(n("retry"));
            queue["next_retry_at"] = json!(stats.next_retry_at);
            queue["max_attempt_count"] = json!(stats.max_attempt_count);
            queue["latest_job_error"] = json!(stats
                .errors
                .iter()
                .find(|e| e.status == "retry")
                .map(|e| e.error.clone()));
            metrics = json!({
                "symbols_created": stats.entities,
                "relationships_created": stats.relationships,
                "documents_processed": stats.documents,
                "chunks": stats.chunks,
                "links": stats.links,
                "database_size_bytes": 0,
            });
            for e in &stats.errors {
                let v = json!({
                    "source_id": source.id,
                    "path": e.rel_path,
                    "error_code": e.status,
                    "error_message": e.error,
                    "occurred_at": f("last_scan_at").map(|m| millis_to_iso(&m)),
                });
                if last_error_detail.is_null() {
                    last_error_detail = v.clone();
                }
                errors.push(v);
            }
        }
        let online = f("online_status").unwrap_or_else(|| "unknown".into());
        let last_scan = f("last_scan_at").map(|m| millis_to_iso(&m));
        let last_error = f("last_error");
        let access = st::access_state(source.enabled, &online);
        let failed_files = counts.get("failed").and_then(Value::as_i64).unwrap_or(0);
        let index_state = st::derive_index_state(
            access,
            &queue,
            failed_files,
            last_error.as_deref(),
            last_scan.as_deref(),
            &indexer,
            &source.id,
        );
        let lease_live = f("lease_expires_at")
            .and_then(|e| e.parse::<i64>().ok())
            .is_some_and(|e| e > chrono::Utc::now().timestamp_millis());
        builds.push(json!({
            "source_id": source.id,
            "active_build_id": active,
            "pending_build_id": f("pending_build_id"),
            "build_state": f("state"),
            "retired_builds": state.as_ref().map(|s| s.doc["retired_builds"].clone()),
            "writer": if lease_live { json!(f("lease_owner")) } else { Value::Null },
        }));
        rows.push(json!({
            "id": source.id,
            "path": source.path,
            "type": serde_json::to_value(source.source_type).unwrap_or(Value::Null),
            "enabled": source.enabled,
            "status": online,
            "counts": counts,
            "queue_depth": queue["depth"],
            "last_scan_at": last_scan,
            "last_error": last_error,
            "access_state": access,
            "index_state": index_state,
            "queue": queue,
            "last_activity_at": last_scan,
            "last_error_detail": last_error_detail,
            "metrics": metrics,
            "active_build_id": active,
        }));
    }
    errors.truncate(RECENT_ERRORS);
    let queues: Vec<Value> = rows.iter().map(|r| r["queue"].clone()).collect();
    let queue = st::merge_queue_stats(&queues);
    let backend = json!({
        "type": server.engine().as_str(),
        "authoritative": true,
        "index_prefix": server.prefix(),
        "cluster_health": health,
        "builds": builds,
    });
    let problems = st::derive_problems(&rows, &indexer, &backend, &queue);
    let mut by_status: BTreeMap<String, i64> = BTreeMap::new();
    for r in &rows {
        for (k, v) in r["counts"].as_object().into_iter().flatten() {
            *by_status.entry(k.clone()).or_default() += v.as_i64().unwrap_or(0);
        }
    }
    let sum = |k: &str| -> i64 {
        rows.iter()
            .map(|r| r["metrics"][k].as_i64().unwrap_or(0))
            .sum()
    };
    let total_files: i64 = by_status.values().sum();
    let get = |k: &str| by_status.get(k).copied().unwrap_or(0);
    let depth: i64 = rows
        .iter()
        .map(|r| r["queue_depth"].as_i64().unwrap_or(0))
        .sum();
    Ok(json!({
        "health": {"status": st::derive_health(&problems), "problem_count": problems.len()},
        "indexer": indexer,
        "queue": queue,
        "recent_errors": errors,
        "recent_error_count": errors.len(),
        "sources_with_errors": rows.iter().filter(|r| st::row_has_errors(r)).count(),
        "problems": problems,
        "sources": rows,
        "backend": backend,
        "totals": {
            "by_status": by_status,
            "queue_depth": depth,
            "metrics": {
                "files_discovered": total_files,
                "files_indexed": get("indexed"),
                "files_failed": get("failed"),
                "symbols_created": sum("symbols_created"),
                "relationships_created": sum("relationships_created"),
                "documents_processed": sum("documents_processed"),
                "index_queue_depth": depth,
                "database_size_bytes": 0,
            },
        },
    }))
}
