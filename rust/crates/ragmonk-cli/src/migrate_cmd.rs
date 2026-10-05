//! `ragmonk migrate-to-rust-v2 --check | --import-sources | --execute`
//! (RUST-15).
//!
//! `--check` is read-only. It reports what is preserved (config and
//! source definitions), the V1 state that is ignored, the V2 local index
//! state `--execute` would reset and, in server mode, the legacy V1
//! server indexes it would delete. It ends with a plan fingerprint.
//!
//! `--execute` is the destructive step. It needs confirmation: type
//! `MIGRATE` at the prompt, or pass `--confirm <fingerprint>` from a check
//! of the same plan. It then:
//!
//! 1. imports the V1 source definitions (idempotent; V1 files untouched);
//! 2. in server mode, deletes the exact legacy V1 indexes and creates or
//!    verifies the V2 indexes;
//! 3. deletes all V2 index-derived local state (`v2/projects/*`);
//! 4. marks every source for a full V2 rebuild.
//!
//! No side-by-side legacy-index rollback is kept. Going back to the
//! Python release afterwards means rebuilding its V1 indexes from the
//! original sources.

use std::io::{IsTerminal, Write};

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::control::ControlPlane;
use ragmonk_storage::preflight::{self, PreflightReport};
use ragmonk_storage::V2Layout;
use serde_json::{json, Value};

use crate::{load, prepared_home, print_json};

pub const CONFIRM_WORD: &str = "MIGRATE";
const RESET_REASON: &str = "migrate-to-rust-v2";

#[derive(clap::Args)]
#[command(group(clap::ArgGroup::new("action").required(true).args(["check", "import_sources", "execute"])))]
pub struct MigrateArgs {
    /// Read-only report: preserved source/config definitions, ignored V1
    /// state, the V2 state and legacy server indexes --execute would
    /// delete, and the plan fingerprint.
    #[arg(long)]
    check: bool,
    /// Import V1 source definitions into the V2 control plane and mark
    /// every source for a full V2 rebuild. Never modifies or deletes V1 data.
    #[arg(long = "import-sources")]
    import_sources: bool,
    /// Destructive: delete legacy server indexes and V2 local index state,
    /// initialize V2 and mark every source for a full rebuild.
    #[arg(long)]
    execute: bool,
    /// The plan fingerprint from --check (skips the interactive prompt).
    #[arg(long, requires = "execute")]
    confirm: Option<String>,
    #[arg(long = "json")]
    json: bool,
}

fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

/// The server half of the plan.
fn server_plan(home: &Home) -> Result<Option<Value>, RagMonkError> {
    let cfg = load(home)?;
    if cfg.storage.mode != "server" {
        return Ok(None);
    }
    let server = &cfg.storage.server;
    let backend = ragmonk_backends::ServerBackend::connect(server, None)?;
    let report = ragmonk_backends::legacy::discover(backend.client(), &server.index_prefix)?;
    let engine = if server.engine.as_str() == "elasticsearch" {
        ragmonk_backends::engine::Engine::Elasticsearch
    } else {
        ragmonk_backends::engine::Engine::OpenSearch
    };
    let manifest = ragmonk_backends::schema::manifest(
        &ragmonk_backends::schema::v2_prefix(&server.index_prefix),
        engine,
        Some(&ragmonk_backends::engine::default_vector_spec()),
    );
    Ok(Some(json!({
        "engine": server.engine.as_str(),
        "legacy_indexes": report.indexes,
        "legacy_fingerprint": report.fingerprint,
        "v2_indexes": manifest["indexes"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|i| i["name"].as_str())
            .collect::<Vec<_>>(),
    })))
}

/// V2 project directories `--execute` deletes.
fn local_reset(home: &Home) -> Vec<String> {
    let dir = V2Layout::new(home).root().join("projects");
    let mut out: Vec<String> = std::fs::read_dir(&dir)
        .map(|entries| {
            entries
                .flatten()
                .filter(|e| e.path().is_dir())
                .map(|e| e.path().display().to_string())
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

/// Every source definition the migration keeps: V1 and V2 ones.
fn source_ids(home: &Home, report: &PreflightReport) -> Result<Vec<String>, RagMonkError> {
    let mut ids: Vec<String> = report
        .sources
        .iter()
        .map(|s| format!("{} {}", s.id, s.path))
        .collect();
    // Read-only: a check never opens (and so never migrates) the control plane.
    for (id, path) in
        ragmonk_storage::maintenance::registered_sources(&V2Layout::new(home).control_db())
    {
        ids.push(format!("{id} {path}"));
    }
    ids.sort();
    ids.dedup();
    Ok(ids)
}

/// The full plan, with its fingerprint.
fn plan(home: &Home) -> Result<(PreflightReport, Value), RagMonkError> {
    let report = preflight::preflight(home)?;
    let server = server_plan(home)?;
    let reset = local_reset(home);
    let sources = source_ids(home, &report)?;
    let basis = json!({
        "sources": sources,
        "local_reset": reset,
        "legacy": server.as_ref().map(|s| s["legacy_fingerprint"].clone()),
    });
    let fingerprint = ragmonk_core::ids::sha256_hex(basis.to_string().as_bytes())[..16].to_owned();
    Ok((
        report,
        json!({
            "local_reset": reset,
            "server": server,
            "sources_marked_for_full_rebuild": sources,
            "fingerprint": fingerprint,
            "rollback": "No legacy-index rollback is kept: returning to the Python release after \
                         --execute requires rebuilding its V1 indexes from the original sources.",
        }),
    ))
}

fn print_plan(execute: &Value) {
    println!("--execute would:");
    let reset = execute["local_reset"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    println!(
        "  - delete V2 local index state ({} project director{})",
        reset.len(),
        if reset.len() == 1 { "y" } else { "ies" }
    );
    for p in &reset {
        println!("      {}", p.as_str().unwrap_or_default());
    }
    if let Some(s) = execute["server"].as_object() {
        let legacy = s["legacy_indexes"].as_array().cloned().unwrap_or_default();
        println!(
            "  - delete {} legacy V1 {} index(es):",
            legacy.len(),
            s["engine"].as_str().unwrap_or_default()
        );
        for i in &legacy {
            println!(
                "      {} ({} docs)",
                i["name"].as_str().unwrap_or_default(),
                i["docs"]
            );
        }
        println!(
            "  - create/verify {} V2 index(es)",
            s["v2_indexes"].as_array().map_or(0, Vec::len)
        );
    }
    let sources = execute["sources_marked_for_full_rebuild"]
        .as_array()
        .map_or(0, Vec::len);
    println!("  - mark {sources} source(s) for a full V2 rebuild");
    println!(
        "Rollback: {}",
        execute["rollback"].as_str().unwrap_or_default()
    );
    println!(
        "Plan fingerprint: {}",
        execute["fingerprint"].as_str().unwrap_or_default()
    );
}

fn check(home: &Home, json_output: bool) -> Result<(), RagMonkError> {
    let (report, execute) = plan(home)?;
    if json_output {
        let mut v = serde_json::to_value(&report).map_err(generic)?;
        v["execute"] = execute;
        return print_json(&v);
    }
    crate::print_preflight(&report);
    print_plan(&execute);
    Ok(())
}

fn confirmed(execute: &Value, confirm: Option<&str>) -> Result<(), RagMonkError> {
    let fingerprint = execute["fingerprint"].as_str().unwrap_or_default();
    if let Some(given) = confirm {
        if given == fingerprint {
            return Ok(());
        }
        return Err(RagMonkError::usage(format!(
            "the migration plan changed since the check (fingerprint {fingerprint} != {given}); \
             re-run 'ragmonk migrate-to-rust-v2 --check' and review it; nothing was changed"
        )));
    }
    if !std::io::stdin().is_terminal() {
        return Err(RagMonkError::usage(
            "--execute is destructive: run it interactively, or pass --confirm <fingerprint> \
             from 'ragmonk migrate-to-rust-v2 --check'; nothing was changed",
        ));
    }
    print_plan(execute);
    print!("This cannot be undone. Type {CONFIRM_WORD} to continue: ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().read_line(&mut line);
    if line.trim() == CONFIRM_WORD {
        Ok(())
    } else {
        Err(RagMonkError::usage(
            "aborted: not confirmed; nothing was changed",
        ))
    }
}

fn execute(home: &Home, confirm: Option<&str>, json_output: bool) -> Result<(), RagMonkError> {
    let (_, planned) = plan(home)?;
    confirmed(&planned, confirm)?;
    if let Some(info) = ragmonk_indexing::daemon::pid::running_daemon(home) {
        return Err(RagMonkError::usage(format!(
            "the daemon is running (pid {}); stop it first with 'ragmonk daemon stop'; nothing \
             was changed",
            info.pid
        )));
    }
    let cfg = load(home)?;
    let lock = RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        "migrate-to-rust-v2",
        None,
        std::time::Duration::from_secs_f64(cfg.indexing.lock_timeout_seconds),
    )?;
    let outcome = (|| -> Result<Value, RagMonkError> {
        // 1. Source definitions (non-destructive, idempotent).
        let imported = preflight::import_sources(home, cfg.runtime.sqlite_cache_size_mb)?;
        // 2. Server indexes.
        let mut server = Value::Null;
        if let Some(s) = planned["server"].as_object() {
            let conf = &cfg.storage.server;
            let backend = ragmonk_backends::ServerBackend::connect(
                conf,
                Some(ragmonk_backends::engine::default_vector_spec()),
            )?;
            let deleted = ragmonk_backends::legacy::delete_legacy(
                backend.client(),
                &conf.index_prefix,
                s["legacy_fingerprint"].as_str().unwrap_or_default(),
            )?;
            let init = backend.init()?;
            server = json!({
                "legacy_deleted": deleted,
                "v2_created": init.created,
                "v2_verified": init.existing,
            });
        }
        // 3. V2 index-derived local state.
        let mut reset = Vec::new();
        for dir in planned["local_reset"].as_array().into_iter().flatten() {
            let dir = dir.as_str().unwrap_or_default();
            std::fs::remove_dir_all(dir)
                .map_err(|e| generic(format!("failed to delete {dir}: {e}")))?;
            reset.push(dir.to_owned());
        }
        // 4. Every source starts over.
        let layout = V2Layout::new(home);
        let (mut cp, _) =
            ControlPlane::open(&layout, cfg.runtime.sqlite_cache_size_mb).map_err(generic)?;
        let mut marked = Vec::new();
        for s in cp.list_sources(false).map_err(generic)? {
            cp.reset_index_state(&s.id, RESET_REASON).map_err(generic)?;
            marked.push(s.id);
        }
        let report = json!({
            "imported": imported.imported,
            "already_present": imported.already_present,
            "control_backup": imported.control_backup,
            "server": server,
            "local_reset": reset,
            "needs_full_rebuild": marked,
            "next": "run 'ragmonk index' to build every source with Rust V2",
        });
        cp.log("migrate_to_rust_v2_execute", &report)
            .map_err(generic)?;
        Ok(report)
    })();
    lock.release();
    let report = outcome?;
    if json_output {
        return print_json(&report);
    }
    println!(
        "Imported {} source definition(s); {} already present.",
        report["imported"].as_array().map_or(0, Vec::len),
        report["already_present"].as_array().map_or(0, Vec::len)
    );
    if let Some(s) = report["server"].as_object() {
        println!(
            "Deleted {} legacy index(es); created {} and verified {} V2 index(es).",
            s["legacy_deleted"].as_array().map_or(0, Vec::len),
            s["v2_created"].as_array().map_or(0, Vec::len),
            s["v2_verified"].as_array().map_or(0, Vec::len)
        );
    }
    println!(
        "Reset V2 local index state ({} project director(ies)).",
        report["local_reset"].as_array().map_or(0, Vec::len)
    );
    println!(
        "{} source(s) marked for a full V2 rebuild. Next: ragmonk index",
        report["needs_full_rebuild"].as_array().map_or(0, Vec::len)
    );
    Ok(())
}

pub fn run(args: &MigrateArgs) -> Result<(), RagMonkError> {
    if args.check {
        return check(&Home::discover(), args.json);
    }
    let home = prepared_home()?;
    if args.execute {
        return execute(&home, args.confirm.as_deref(), args.json);
    }
    let cache = load(&home)?.runtime.sqlite_cache_size_mb;
    let result = preflight::import_sources(&home, cache)?;
    if args.json {
        return print_json(&result);
    }
    println!(
        "Imported {} source definition(s); {} already present.",
        result.imported.len(),
        result.already_present.len()
    );
    if let Some(b) = &result.control_backup {
        println!("Backed up the V2 control plane to {b}");
    }
    println!(
        "{} source(s) require a full Rust V2 rebuild.",
        result.needs_full_rebuild.len()
    );
    Ok(())
}
