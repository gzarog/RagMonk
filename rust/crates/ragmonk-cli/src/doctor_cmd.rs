//! `ragmonk doctor [--json]` and `ragmonk health [--json]`
//! (`cli/doctor.py`). Same sections, check names and verdict rules as
//! the reference, over the V2 storage. The AI-provider section is not
//! ported, because AI features are outside RUST-12.

use std::path::Path;

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::lock::{inspect_lock, LockState};
use ragmonk_indexing::scan::check_root_accessible;
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::maintenance;
use ragmonk_storage::V2Layout;
use serde_json::{json, Value};

use crate::workflow::control_plane;
use crate::{load, prepared_home, print_json};

const LOW_DISK_WARN_GB: f64 = 1.0;
const QUEUE_DEPTH_WARN: i64 = 1000;

#[derive(Debug, Clone)]
pub struct Check {
    pub name: &'static str,
    /// `ok`, `warn` or `fail`.
    pub status: &'static str,
    pub detail: String,
}

#[derive(Debug, Clone)]
pub struct Section {
    pub name: &'static str,
    pub checks: Vec<Check>,
}

fn check(name: &'static str, status: &'static str, detail: impl Into<String>) -> Check {
    Check {
        name,
        status,
        detail: detail.into(),
    }
}

/// `UNHEALTHY` on any fail, `HEALTHY WITH WARNINGS` on any warn.
pub fn overall(sections: &[Section]) -> &'static str {
    let any = |s: &str| {
        sections
            .iter()
            .flat_map(|x| &x.checks)
            .any(|c| c.status == s)
    };
    if any("fail") {
        "UNHEALTHY"
    } else if any("warn") {
        "HEALTHY WITH WARNINGS"
    } else {
        "HEALTHY"
    }
}

fn free_gb(path: &Path) -> Option<f64> {
    let disks = sysinfo::Disks::new_with_refreshed_list();
    let path = ragmonk_core::paths::resolve(path).ok()?;
    disks
        .list()
        .iter()
        .filter(|d| path.starts_with(d.mount_point()))
        .max_by_key(|d| d.mount_point().as_os_str().len())
        .map(|d| d.available_space() as f64 / 1024f64.powi(3))
}

pub fn run_checks(home: &Home) -> Result<Vec<Section>, RagMonkError> {
    let cfg = load(home)?;
    let cp = control_plane(home)?;
    let layout = V2Layout::new(home);
    let db =
        |e: ragmonk_storage::StorageError| RagMonkError::new(ErrorKind::Database, e.to_string());
    let mut sections = vec![Section {
        name: "Core",
        checks: vec![check(
            "version",
            "ok",
            format!("version {}", ragmonk_core::version::version()),
        )],
    }];

    let journal = maintenance::journal_mode(cp.connection());
    let version = maintenance::schema_version(&layout.control_db()).unwrap_or(0);
    let latest = maintenance::latest_control_version();
    sections.push(Section {
        name: "Database",
        checks: vec![
            check("sqlite", "ok", "SQLite"),
            check(
                "wal",
                if journal == "wal" { "ok" } else { "warn" },
                format!("WAL ({journal})"),
            ),
            check(
                "schema",
                if version == latest { "ok" } else { "fail" },
                format!("schema v{version}"),
            ),
        ],
    });

    let sources = cp.list_sources(true).map_err(db)?;
    let mut unreachable = 0;
    for s in &sources {
        let offline = cp.state(&s.id).map_err(db)?.online_status == "offline";
        if offline || check_root_accessible(Path::new(&s.path)).is_some() {
            unreachable += 1;
        }
    }
    sections.push(Section {
        name: "Sources",
        checks: vec![check(
            "reachability",
            if unreachable > 0 { "warn" } else { "ok" },
            format!(
                "{}/{} reachable",
                sources.len() - unreachable,
                sources.len()
            ),
        )],
    });

    let lock = match inspect_lock(&home.locks_dir().join("index.lock")) {
        LockState::Free => check("index_lock", "ok", "free"),
        LockState::Unknown => check("index_lock", "warn", "unknown"),
        LockState::Held(owner) => {
            let mut parts = vec!["held".to_owned()];
            if let Some(o) = owner {
                if let Some(p) = o.pid {
                    parts.push(format!("PID {p}"));
                }
                if let Some(op) = o.operation {
                    parts.push(format!("operation {op}"));
                }
                if let Some(sid) = o.source_id {
                    parts.push(format!("source {sid}"));
                }
            }
            check("index_lock", "warn", parts.join(", "))
        }
    };
    sections.push(Section {
        name: "Index Lock",
        checks: vec![lock],
    });

    let status = ragmonk_indexing::status::collect_status(
        home,
        &cp,
        cfg.indexing.status_stall_threshold_seconds,
        chrono::Utc::now(),
    )
    .map_err(db)?;
    let depth = status["totals"]["queue_depth"].as_i64().unwrap_or(0);
    sections.push(Section {
        name: "Index",
        checks: vec![check(
            "queue",
            if depth < QUEUE_DEPTH_WARN {
                "ok"
            } else {
                "warn"
            },
            if depth == 0 {
                "queue empty".to_owned()
            } else {
                format!("queue depth {depth}")
            },
        )],
    });

    let disk = match free_gb(home.root()) {
        Some(gb) => check(
            "free_space",
            if gb >= LOW_DISK_WARN_GB { "ok" } else { "warn" },
            format!("{gb:.0} GB free"),
        ),
        None => check("free_space", "warn", "free space unknown"),
    };
    sections.push(Section {
        name: "Disk",
        checks: vec![disk],
    });

    // Every enabled source's active build.
    let mut stores = Vec::new();
    for s in &sources {
        if let Some(build) = cp.state(&s.id).map_err(db)?.active_build_id {
            let pid = project_id_for_canonical(&s.path);
            let (store, _) =
                ProjectStore::open(&layout, &pid, &s.id, cfg.runtime.sqlite_cache_size_mb)
                    .map_err(db)?;
            stores.push((pid, store, build));
        }
    }

    if cfg.search.semantic {
        let fp = ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL.fingerprint();
        let mut vectors = 0usize;
        let mut bytes = 0u64;
        for (pid, store, build) in &stores {
            vectors += store.embedding_keys(build, &fp).map_err(db)?.len();
            let index = ragmonk_ml::ann::index_path(&layout.project_dir(pid));
            bytes += std::fs::metadata(index).map(|m| m.len()).unwrap_or(0);
        }
        sections.push(Section {
            name: "Semantic",
            checks: vec![check(
                "ann_backend",
                "ok",
                format!(
                    "backend hnsw, {vectors} vector(s), {:.1} MB index",
                    bytes as f64 / (1024.0 * 1024.0)
                ),
            )],
        });
    }

    sections.push(tokenizer_section(&cfg, &stores)?);
    sections.push(ai_section(&cfg));

    if cfg.storage.mode == "server" {
        sections.push(server_section(&cfg));
    }
    Ok(sections)
}

/// The configured AI provider and privacy posture; offline and
/// secret-free, never more than a warning (`_ai_section`).
fn ai_section(cfg: &ragmonk_config::RagMonkConfig) -> Section {
    let provider = &cfg.ai.provider;
    let allowed = cfg.privacy.external_ai_allowed;
    let mut checks = vec![check(
        "provider",
        "ok",
        format!(
            "provider={provider}, external_ai_allowed={}",
            if allowed { "True" } else { "False" }
        ),
    )];
    if let Some(cap) = ragmonk_ai::registry::get(provider).filter(|c| c.subscription) {
        if cap.cloud_egress && !allowed {
            checks.push(check(
                "privacy",
                "warn",
                format!(
                    "{provider} is a cloud provider but privacy.external_ai_allowed=false; set it \
                     true to use it"
                ),
            ));
        }
        let found = |exe| ragmonk_ai::runtime::which(exe).is_some();
        checks.push(match cap.provider_id {
            "codex" if found("codex") => check("runtime", "ok", "codex runtime found on PATH"),
            "codex" => check(
                "runtime",
                "warn",
                "codex runtime not found on PATH; run 'ragmonk ai login codex' setup",
            ),
            // The Rust adapter drives the Copilot CLI (ADR 0026).
            _ if found("copilot") => check("runtime", "ok", "Copilot CLI found on PATH"),
            _ => check(
                "runtime",
                "warn",
                "Copilot CLI not found on PATH; install it and sign in with `copilot`",
            ),
        });
    }
    Section { name: "AI", checks }
}

fn tokenizer_section(
    cfg: &ragmonk_config::RagMonkConfig,
    stores: &[(String, ProjectStore, String)],
) -> Result<Section, RagMonkError> {
    use ragmonk_documents::tokenizer as t;
    let fp = t::tokenizer_fingerprint();
    let mut checks = vec![
        check("model", "ok", t::EMBEDDING_MODEL_ID),
        check(
            "revision",
            "ok",
            format!("revision {}", &t::TOKENIZER_REVISION[..12]),
        ),
        check("fingerprint", "ok", fp.chars().take(19).collect::<String>()),
        check(
            "limits",
            "ok",
            format!(
                "model max {} tokens, chunk ceiling {}",
                t::MAX_SEQUENCE_TOKENS,
                cfg.documents.chunking.resolved_max_tokens()
            ),
        ),
    ];
    let tok = match t::model_tokenizer() {
        Ok(tok) => tok,
        Err(e) => {
            checks.push(check(
                "payloads",
                "fail",
                format!("tokenizer unavailable: {e}"),
            ));
            return Ok(Section {
                name: "Tokenizer",
                checks,
            });
        }
    };
    let (mut scanned, mut max, mut truncated) = (0usize, 0i64, 0usize);
    let mut seen = std::collections::HashSet::new();
    for (pid, store, build) in stores {
        if !seen.insert(pid.clone()) {
            continue;
        }
        let mut stmt = store
            .connection()
            .prepare(
                "SELECT embedding_text FROM chunks
                 WHERE build_id = ?1 AND embedding_text IS NOT NULL AND embedding_text <> ''",
            )
            .map_err(|e| RagMonkError::new(ErrorKind::Database, e.to_string()))?;
        let texts = stmt
            .query_map([build], |r| r.get::<_, String>(0))
            .map_err(|e| RagMonkError::new(ErrorKind::Database, e.to_string()))?;
        for text in texts.flatten() {
            let n = tok.count(&text, true);
            scanned += 1;
            max = max.max(n);
            if n > t::MAX_SEQUENCE_TOKENS {
                truncated += 1;
            }
        }
    }
    checks.push(if scanned == 0 {
        check("payloads", "ok", "no embedding payloads indexed yet")
    } else {
        check(
            "payloads",
            if truncated > 0 { "fail" } else { "ok" },
            format!(
                "{scanned} scanned, max {max}/{} tokens, truncated {truncated}",
                t::MAX_SEQUENCE_TOKENS
            ),
        )
    });
    Ok(Section {
        name: "Tokenizer",
        checks,
    })
}

fn server_section(cfg: &ragmonk_config::RagMonkConfig) -> Section {
    let s = &cfg.storage.server;
    let mut checks = vec![check(
        "engine",
        "ok",
        format!(
            "engine={}, endpoint={}, index_prefix={}",
            s.engine.as_str(),
            ragmonk_telemetry::redact::redact_urls_in_text(&s.url),
            s.index_prefix
        ),
    )];
    let redact =
        |e: &dyn std::fmt::Display| ragmonk_telemetry::redact::redact_urls_in_text(&e.to_string());
    match ragmonk_backends::ServerBackend::connect(s, None) {
        Err(e) => {
            checks.push(check(
                "connectivity",
                "fail",
                format!("server unreachable: {}", redact(&e)),
            ));
            checks.push(check("indices", "fail", "skipped -- server unreachable"));
        }
        Ok(backend) => {
            checks.push(check(
                "connectivity",
                "ok",
                format!("reachable ({})", backend.engine().as_str()),
            ));
            match backend.index_status() {
                Ok(statuses) => {
                    let missing: Vec<&str> = statuses
                        .iter()
                        .filter(|(_, exists)| !exists)
                        .map(|(n, _)| n.as_str())
                        .collect();
                    let n = statuses.len();
                    checks.push(if missing.is_empty() {
                        check("indices", "ok", format!("{n}/{n} indices present"))
                    } else {
                        check(
                            "indices",
                            "warn",
                            format!(
                                "{}/{n} indices present; missing: {} (run 'ragmonk server-v2 init' to create)",
                                n - missing.len(),
                                missing.join(", ")
                            ),
                        )
                    });
                }
                Err(e) => checks.push(check(
                    "indices",
                    "fail",
                    format!("could not check indices: {}", redact(&e)),
                )),
            }
        }
    }
    Section {
        name: "Server",
        checks,
    }
}

fn sections_json(sections: &[Section], result: &str) -> Value {
    json!({
        "result": result,
        "sections": sections.iter().map(|s| json!({
            "name": s.name,
            "checks": s.checks.iter().map(|c| json!({
                "name": c.name, "status": c.status, "detail": c.detail,
            })).collect::<Vec<_>>(),
        })).collect::<Vec<_>>(),
    })
}

/// Unhealthy: exit with the health-check code and no extra error line.
fn verdict(result: &str) -> Result<(), RagMonkError> {
    if result == "UNHEALTHY" {
        return Err(RagMonkError::new(ErrorKind::HealthCheck, ""));
    }
    Ok(())
}

pub fn doctor(json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let sections = run_checks(&home)?;
    let result = overall(&sections);
    if json_output {
        print_json(&sections_json(&sections, result))?;
    } else {
        println!("\nRagMonk Doctor\n");
        for s in &sections {
            println!("{}", s.name);
            for c in &s.checks {
                println!("  {} {}", c.status.to_uppercase(), c.detail);
            }
            println!();
        }
        println!("Result:\n  {result}");
    }
    verdict(result)
}

pub fn health(json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let result = overall(&run_checks(&home)?);
    if json_output {
        print_json(&json!({ "result": result }))?;
    } else {
        println!("{result}");
    }
    verdict(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verdict_rules() {
        let s = |st: &'static str| Section {
            name: "x",
            checks: vec![check("c", st, "")],
        };
        assert_eq!(overall(&[s("ok")]), "HEALTHY");
        assert_eq!(overall(&[s("ok"), s("warn")]), "HEALTHY WITH WARNINGS");
        assert_eq!(overall(&[s("warn"), s("fail")]), "UNHEALTHY");
    }
}
