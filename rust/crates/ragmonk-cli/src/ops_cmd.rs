//! Operations (RUST-12 slice 3): `backup`, `restore`, `rebuild`,
//! `upgrade`, `uninstall` and `vectors rebuild|backfill`.
//!
//! Archives are V2-native (ADR 0024): a `.tar.gz` holding
//! `manifest.json`, a consistent `VACUUM INTO` snapshot of
//! `v2/control.db` and of every indexed project's
//! `v2/projects/<id>/knowledge.db`, plus `config.yaml`. Manifest keys
//! follow the reference. `format_version` 2 and `storage: "v2"` mark the
//! layout. Python V1 archives are refused with directions. ANN index
//! files are caches and are not archived: queries fall back to exact
//! search until the next sync rebuilds them.

use std::path::{Path, PathBuf};
use std::time::Duration;

use clap::Subcommand;
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_indexing::coordinator::{run_source, Options};
use ragmonk_indexing::daemon::pid;
use ragmonk_indexing::lock::RunLock;
use ragmonk_storage::maintenance;
use ragmonk_storage::V2Layout;
use serde_json::{json, Value};

use crate::query_cmd::open_sources;
use crate::workflow::control_plane;
use crate::{load, prepared_home, print_json};

pub const MANIFEST_FORMAT_VERSION: i64 = 2;
const STOP_TIMEOUT: Duration = Duration::from_secs(15);

fn generic(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Generic, e.to_string())
}

fn dberr(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

fn index_lock(home: &Home, operation: &str) -> Result<RunLock, RagMonkError> {
    let cfg = load(home)?;
    RunLock::acquire(
        &home.locks_dir().join("index.lock"),
        operation,
        None,
        Duration::from_secs_f64(cfg.indexing.lock_timeout_seconds),
    )
}

fn now_iso() -> String {
    ragmonk_indexing::progress::now_iso()
}

/// A private scratch directory under `<home>/tmp`, removed on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new(home: &Home, label: &str) -> Result<Self, RagMonkError> {
        let stamp = chrono::Utc::now().format("%Y%m%d%H%M%S%f");
        let dir = home
            .tmp_dir()
            .join(format!("{label}_{stamp}_{}", std::process::id()));
        std::fs::create_dir_all(&dir).map_err(generic)?;
        Ok(Self(dir))
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

// ------------------------------------------------------------- backup ---

/// Writes a V2 backup archive and returns `(path, manifest)`. Reads every
/// database through read-only snapshots, so it never migrates anything
/// (`upgrade` relies on that).
pub fn create_backup(home: &Home, dest: Option<&Path>) -> Result<(PathBuf, Value), RagMonkError> {
    let layout = V2Layout::new(home);
    let control = layout.control_db();
    let mut projects = serde_json::Map::new();
    let mut sources = Vec::new();
    for (id, path) in maintenance::registered_sources(&control) {
        let pid = project_id_for_canonical(&path);
        if let Some(v) = maintenance::schema_version(&layout.project_db(&pid)) {
            projects.insert(pid.clone(), json!(v));
        }
        sources.push(json!({"id": id, "path": path, "project_id": pid}));
    }
    let created_at = now_iso();
    let manifest = json!({
        "format_version": MANIFEST_FORMAT_VERSION,
        "storage": "v2",
        "ragmonk_version": ragmonk_core::version::version(),
        "created_at": created_at,
        "sources_schema_version": maintenance::schema_version(&control).unwrap_or(0),
        "projects": projects,
        "sources": sources,
    });
    let archive = match dest {
        Some(d) => d.to_path_buf(),
        None => {
            let stamp: String = created_at
                .chars()
                .filter(|c| !matches!(c, ':' | '-' | '.'))
                .collect();
            home.backups_dir()
                .join(format!("ragmonk-backup-{stamp}.tar.gz"))
        }
    };
    if let Some(dir) = archive.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(dir).map_err(generic)?;
    }
    let scratch = Scratch::new(home, "backup")?;
    let stage = &scratch.0;
    if control.is_file() {
        maintenance::snapshot(&control, &stage.join("v2").join("control.db")).map_err(dberr)?;
    }
    for pid in projects.keys() {
        maintenance::snapshot(
            &layout.project_db(pid),
            &stage
                .join("v2")
                .join("projects")
                .join(pid)
                .join("knowledge.db"),
        )
        .map_err(dberr)?;
    }
    if home.user_config().is_file() {
        std::fs::copy(home.user_config(), stage.join("config.yaml")).map_err(generic)?;
    }
    std::fs::write(
        stage.join("manifest.json"),
        serde_json::to_string_pretty(&manifest).map_err(generic)?,
    )
    .map_err(generic)?;
    let file = std::fs::File::create(&archive).map_err(generic)?;
    let gz = flate2::write::GzEncoder::new(file, flate2::Compression::default());
    let mut tar = tar::Builder::new(gz);
    tar.follow_symlinks(false);
    tar.append_dir_all(".", stage).map_err(generic)?;
    tar.into_inner()
        .and_then(flate2::write::GzEncoder::finish)
        .map_err(generic)?;
    Ok((archive, manifest))
}

pub fn backup(dest: Option<String>, json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    control_plane(&home)?;
    let lock = index_lock(&home, "backup")?;
    let result = create_backup(&home, dest.as_deref().map(Path::new));
    lock.release();
    let (archive, m) = result?;
    let data = json!({
        "archive": archive.to_string_lossy(),
        "ragmonk_version": m["ragmonk_version"],
        "created_at": m["created_at"],
        "sources_schema_version": m["sources_schema_version"],
        "projects": m["projects"],
    });
    if json_output {
        return print_json(&data);
    }
    println!("Backup created {}", archive.display());
    println!(
        "Projects included: {}",
        m["projects"].as_object().map_or(0, serde_json::Map::len)
    );
    Ok(())
}

// ------------------------------------------------------------ restore ---

fn extract(archive: &Path, into: &Path) -> Result<(), RagMonkError> {
    let file = std::fs::File::open(archive).map_err(generic)?;
    let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(file));
    tar.set_preserve_permissions(false);
    // `unpack` refuses entries that would escape `into` (`..`, absolute).
    tar.unpack(into)
        .map_err(|e| dberr(format!("archive is corrupt or unreadable: {e}")))
}

fn move_aside(path: &Path, holding: &Path) -> Result<Option<PathBuf>, RagMonkError> {
    if !path.exists() {
        return Ok(None);
    }
    let target = holding.join(path.file_name().unwrap_or_default());
    std::fs::rename(path, &target).map_err(generic)?;
    Ok(Some(target))
}

pub fn restore(archive: &str, json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let archive = PathBuf::from(archive);
    let report = restore_archive(&home, &archive)?;
    if json_output {
        return print_json(&report);
    }
    let restored = report["projects_restored"].as_array().map_or(0, Vec::len);
    let daemon_was_running = report["daemon_was_running"] == true;
    println!("Restored from {}", archive.display());
    println!("Projects restored: {restored}");
    if daemon_was_running {
        println!(
            "Daemon was running before restore; {}",
            if report["daemon_restarted"] == true {
                "restarted"
            } else {
                "left stopped"
            }
        );
    }
    Ok(())
}

/// Verifies and restores a V2 archive into `home` (the `restore` report).
pub fn restore_archive(home: &Home, archive: &Path) -> Result<Value, RagMonkError> {
    let home = home.clone();
    let archive = archive.to_path_buf();
    if !archive.is_file() {
        return Err(RagMonkError::usage(format!(
            "no such archive: {}",
            archive.display()
        )));
    }
    let scratch = Scratch::new(&home, "restore_staging")?;
    let stage = &scratch.0;
    extract(&archive, stage)?;
    let manifest: Value = std::fs::read_to_string(stage.join("manifest.json"))
        .map_err(|_| dberr("archive is missing manifest.json; refusing to restore"))
        .and_then(|t| {
            serde_json::from_str(&t)
                .map_err(|e| dberr(format!("archive manifest is unreadable: {e}")))
        })?;
    let format = manifest["format_version"].as_i64();
    if manifest["storage"] != "v2" && format.is_some_and(|v| v < MANIFEST_FORMAT_VERSION) {
        return Err(dberr(
            "this is a Python (V1) RagMonk backup; Rust V2 cannot restore it. Restore it with \
             the Python RagMonk, then run 'ragmonk migrate-to-rust-v2 --import-sources'; \
             nothing was changed",
        ));
    }
    match format {
        Some(v) if v <= MANIFEST_FORMAT_VERSION && manifest["storage"] == "v2" => {}
        other => {
            return Err(dberr(format!(
                "backup archive format v{} is newer than this RagMonk understands (v{MANIFEST_FORMAT_VERSION}); refusing to restore",
                other.map_or_else(|| "?".to_owned(), |v| v.to_string())
            )))
        }
    }
    let staged_v2 = stage.join("v2");
    let control = staged_v2.join("control.db");
    if !control.is_file() {
        return Err(dberr(
            "archive is missing v2/control.db; refusing to restore",
        ));
    }
    maintenance::integrity_check(&control).map_err(dberr)?;
    let newer = |what: &str, v: i64, latest: i64| {
        dberr(format!(
            "{what} schema v{v} is newer than this RagMonk version understands (v{latest}); refusing to restore"
        ))
    };
    let (cv, cl) = (
        maintenance::schema_version(&control).unwrap_or(0),
        maintenance::latest_control_version(),
    );
    if cv > cl {
        return Err(newer("control.db", cv, cl));
    }
    let mut restored = Vec::new();
    if let Ok(entries) = std::fs::read_dir(staged_v2.join("projects")) {
        let mut dirs: Vec<PathBuf> = entries.flatten().map(|e| e.path()).collect();
        dirs.sort();
        for dir in dirs {
            let db = dir.join("knowledge.db");
            if !db.is_file() {
                continue;
            }
            maintenance::integrity_check(&db).map_err(dberr)?;
            let name = dir
                .file_name()
                .map(|n| n.to_string_lossy().into_owned())
                .unwrap_or_default();
            let (v, l) = (
                maintenance::schema_version(&db).unwrap_or(0),
                maintenance::latest_knowledge_version(),
            );
            if v > l {
                return Err(newer(&format!("project {name}"), v, l));
            }
            restored.push(name);
        }
    }
    // Everything verified: only now touch the live home.
    let daemon_was_running =
        pid::stop_and_wait(&home, STOP_TIMEOUT, "restore into a live runtime directory")
            .map_err(dberr)?;
    let holding = home.tmp_dir().join(format!(
        "pre_restore_{}",
        chrono::Utc::now().format("%Y%m%d%H%M%S%f")
    ));
    std::fs::create_dir_all(&holding).map_err(generic)?;
    let live_v2 = V2Layout::new(&home).root().to_path_buf();
    let staged_config = stage.join("config.yaml");
    let mut aside: Vec<(PathBuf, PathBuf)> = Vec::new();
    if let Some(t) = move_aside(&live_v2, &holding)? {
        aside.push((t, live_v2.clone()));
    }
    if staged_config.is_file() {
        if let Some(t) = move_aside(&home.user_config(), &holding)? {
            aside.push((t, home.user_config()));
        }
    }
    let swap = std::fs::rename(&staged_v2, &live_v2).and_then(|()| {
        if staged_config.is_file() {
            std::fs::rename(&staged_config, home.user_config())
        } else {
            Ok(())
        }
    });
    if let Err(e) = swap {
        for (moved, original) in aside.into_iter().rev() {
            if original.is_dir() {
                let _ = std::fs::remove_dir_all(&original);
            } else {
                let _ = std::fs::remove_file(&original);
            }
            let _ = std::fs::rename(&moved, &original);
        }
        return Err(generic(format!(
            "restore failed; previous state put back: {e}"
        )));
    }
    let _ = std::fs::remove_dir_all(&holding);
    let mut daemon_restarted = false;
    if daemon_was_running {
        crate::daemon_cmd::run(crate::daemon_cmd::DaemonCommand::Start)?;
        daemon_restarted = true;
    }
    Ok(json!({
        "archive": archive.to_string_lossy(),
        "projects_restored": restored,
        "daemon_was_running": daemon_was_running,
        "daemon_restarted": daemon_restarted,
    }))
}

// ------------------------------------------------------------ rebuild ---

/// Full rebuild of one or every enabled source. A V2 full build is
/// written next to the visible one and published only on success, so a
/// failed rebuild leaves the previous index usable. `--fresh` is
/// therefore always the behavior; the flag adds the reference's root
/// reachability check and confirmation.
pub fn rebuild(
    source_id: Option<String>,
    fresh: bool,
    yes: bool,
    json_output: bool,
) -> Result<(), RagMonkError> {
    if fresh && !yes && !json_output {
        print!(
            "rebuild --fresh will re-index every selected source from its source files \
             (the old index is kept as a backup until the rebuild succeeds). Continue? [y/N]: "
        );
        use std::io::Write;
        let _ = std::io::stdout().flush();
        let mut line = String::new();
        let ok = std::io::stdin().read_line(&mut line).is_ok()
            && matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes");
        if !ok {
            return Err(RagMonkError::usage(
                "aborted: rebuild --fresh not confirmed (pass --yes to skip)",
            ));
        }
    }
    let home = prepared_home()?;
    let outcomes = rebuild_sources(&home, source_id.as_deref(), fresh, |_| {})?;
    let failed: i64 = outcomes
        .iter()
        .map(|o| o["failed"].as_i64().unwrap_or(0))
        .sum();
    if json_output {
        print_json(&json!({ "sources": outcomes }))?;
    } else {
        for o in &outcomes {
            println!(
                "{} {}: rebuilt scanned={} indexed={} failed={} linked={}",
                o["id"].as_str().unwrap_or_default(),
                o["path"].as_str().unwrap_or_default(),
                o["scanned"],
                o["indexed"],
                o["failed"],
                o["linked"]
            );
        }
    }
    if failed > 0 {
        return Err(RagMonkError::new(
            ErrorKind::IndexingPartialFailure,
            format!(
                "{failed} file(s) failed to (re)index during rebuild; see 'ragmonk doctor' for details"
            ),
        ));
    }
    Ok(())
}

/// Rebuilds one or every enabled source under the `index` lock and
/// returns one `{id, path, scanned, indexed, failed, linked}` per source.
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
    let layout = V2Layout::new(home);
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
                cp.require_full_rebuild(&s.id, "manual rebuild")
                    .map_err(dberr)?;
                let r = run_source(&layout, &mut cp, s, &registry, &opts, tracker)?;
                let o = json!({
                    "id": s.id,
                    "path": s.path,
                    "scanned": r.counts.scanned,
                    "indexed": r.indexed,
                    "failed": r.failed,
                    "linked": r.linked,
                });
                each(&o);
                out.push(o);
            }
            Ok(out)
        },
    );
    lock.release();
    outcomes
}

// ------------------------------------------------------------ upgrade ---

pub fn upgrade(json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let layout = V2Layout::new(&home);
    let control_before = maintenance::schema_version(&layout.control_db()).unwrap_or(0);
    let mut projects_before = std::collections::BTreeMap::new();
    for (_, path) in maintenance::registered_sources(&layout.control_db()) {
        let pid = project_id_for_canonical(&path);
        if let Some(v) = maintenance::schema_version(&layout.project_db(&pid)) {
            projects_before.insert(pid, v);
        }
    }
    let pending = control_before < maintenance::latest_control_version()
        || projects_before
            .values()
            .any(|v| *v < maintenance::latest_knowledge_version());
    let backup_archive = if pending {
        Some(create_backup(&home, None)?.0)
    } else {
        None
    };
    // Opening applies pending migrations.
    let cfg = load(&home)?;
    let cp = control_plane(&home)?;
    let control_after = maintenance::schema_version(&layout.control_db()).unwrap_or(0);
    let mut databases =
        vec![json!({"name": "control", "before": control_before, "after": control_after})];
    for s in cp.list_sources(false).map_err(dberr)? {
        let pid = project_id_for_canonical(&s.path);
        let Some(before) = projects_before.get(&pid).copied() else {
            continue;
        };
        ragmonk_storage::knowledge::ProjectStore::open(
            &layout,
            &pid,
            &s.id,
            cfg.runtime.sqlite_cache_size_mb,
        )
        .map_err(dberr)?;
        let after = maintenance::schema_version(&layout.project_db(&pid)).unwrap_or(0);
        databases.push(json!({"name": format!("project:{pid}"), "before": before, "after": after}));
    }
    let healthy_after =
        crate::doctor_cmd::overall(&crate::doctor_cmd::run_checks(&home)?) != "UNHEALTHY";
    let archive_str = backup_archive
        .as_ref()
        .map(|p| p.to_string_lossy().into_owned());
    if json_output {
        print_json(&json!({
            "pending": pending,
            "backup_archive": archive_str,
            "databases": databases,
            "healthy_after": healthy_after,
        }))?;
    } else {
        match &archive_str {
            None => println!("Already up to date; no pending migrations."),
            Some(a) => println!("Backup taken before upgrading: {a}"),
        }
        for d in &databases {
            let (b, a) = (d["before"].as_i64(), d["after"].as_i64());
            println!(
                "  {}: v{} {} v{}",
                d["name"].as_str().unwrap_or_default(),
                b.unwrap_or(0),
                if b == a { "==" } else { "->" },
                a.unwrap_or(0)
            );
        }
        if healthy_after {
            println!("Post-upgrade health check: OK");
        } else {
            let hint = archive_str.as_ref().map_or_else(
                || "the most recent 'ragmonk backup' archive".to_owned(),
                |a| format!("'ragmonk restore {a}'"),
            );
            println!(
                "Post-upgrade health check: UNHEALTHY -- see 'ragmonk doctor' for detail. No automatic rollback was attempted; if needed, restore the pre-upgrade backup with {hint}."
            );
        }
    }
    if !healthy_after {
        return Err(RagMonkError::new(ErrorKind::HealthCheck, ""));
    }
    Ok(())
}

// ---------------------------------------------------------- uninstall ---

/// Purges `RAGMONK_HOME` (stopping a running daemon first) unless
/// `--keep-data`. A standalone Rust binary is never deleted by itself
/// (on Windows a running executable cannot be), so its removal is a
/// manual instruction.
pub fn uninstall(keep_data: bool, yes: bool, json_output: bool) -> Result<(), RagMonkError> {
    let home = Home::discover();
    let exe = std::env::current_exe()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "ragmonk".into());
    let manual = format!(
        "this is a standalone RagMonk binary; remove it by deleting {exe} (and any launcher or PATH entry you added)."
    );
    if !yes {
        println!("This will permanently remove:");
        println!("  - the application (see manual instructions below)");
        if !keep_data {
            println!(
                "  - all data under {} (databases, config, backups, logs)",
                home.root().display()
            );
        }
        print!("Proceed? [y/N]: ");
        use std::io::Write;
        let _ = std::io::stdout().flush();
        let mut line = String::new();
        let ok = std::io::stdin().read_line(&mut line).is_ok()
            && matches!(line.trim().to_ascii_lowercase().as_str(), "y" | "yes");
        if !ok {
            println!("Aborted; nothing was removed.");
            return Ok(());
        }
    }
    let mut data_purged = false;
    if !keep_data && home.root().exists() {
        pid::stop_and_wait(&home, STOP_TIMEOUT, "delete this data").map_err(generic)?;
        std::fs::remove_dir_all(home.root()).map_err(generic)?;
        data_purged = true;
    }
    if json_output {
        return print_json(&json!({
            "install_method": "standalone_binary",
            "data_purged": data_purged,
            "app_removed": false,
            "manual_instructions": manual,
        }));
    }
    if data_purged {
        println!("✓ Removed data under {}", home.root().display());
    }
    println!("\n! {manual}");
    Ok(())
}

// ------------------------------------------------------------ vectors ---

#[derive(Subcommand)]
pub enum VectorsCommand {
    /// Rebuild the ANN index from stored vectors.
    Rebuild {
        /// Only rebuild this source id.
        #[arg(long = "source")]
        source: Option<String>,
        #[arg(long = "json")]
        json: bool,
    },
    /// Compute vectors for indexed files that have none yet.
    Backfill {
        /// Only backfill this source id.
        #[arg(long = "source")]
        source: Option<String>,
        #[arg(long = "json")]
        json: bool,
    },
}

pub fn vectors(cmd: VectorsCommand) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let layout = V2Layout::new(&home);
    let spec = ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL;
    let fp = spec.fingerprint();
    match cmd {
        VectorsCommand::Rebuild { source, json: j } => {
            let lock = index_lock(&home, "vectors-rebuild")?;
            let mut rebuilt = Vec::new();
            for o in open_sources(&home, source.as_deref())? {
                let n = o.store.embedding_keys(&o.build, &fp).map_err(dberr)?.len();
                if n == 0 {
                    rebuilt.push(json!({"source_id": o.source.id, "backend": null, "vectors": 0}));
                    continue;
                }
                let dir = layout.project_dir(&project_id_for_canonical(&o.source.path));
                let _ = std::fs::remove_file(ragmonk_ml::ann::index_path(&dir));
                let stats = ragmonk_ml::ann::sync(&o.store, &dir, &o.build, &fp, spec.dims)
                    .map_err(generic)?;
                rebuilt.push(
                    json!({"source_id": o.source.id, "backend": "hnsw", "vectors": stats.live}),
                );
            }
            lock.release();
            if j {
                return print_json(&json!({ "rebuilt": rebuilt }));
            }
            if rebuilt.is_empty() {
                println!("No sources to rebuild.");
            }
            for e in &rebuilt {
                let id = e["source_id"].as_str().unwrap_or_default();
                if e["backend"].is_null() {
                    println!("{id}: no embeddings computed yet.");
                } else {
                    println!(
                        "{id}: rebuilt {} vector(s) via {}",
                        e["vectors"],
                        e["backend"].as_str().unwrap_or_default()
                    );
                }
            }
        }
        VectorsCommand::Backfill { source, json: j } => {
            let cfg = load(&home)?;
            let lazy = ragmonk_ml::LazyEmbedder::new(
                ragmonk_ml::embedder::models_root(Some(&home.root().join("models"))),
                spec,
                cfg.indexing.embedding_batch_size,
            );
            let embedder = lazy.get().map_err(|e| {
                RagMonkError::usage(format!(
                    "the embedding model is not available ({e}); install it first"
                ))
            })?;
            let lock = index_lock(&home, "vectors-backfill")?;
            let mut done = Vec::new();
            for mut o in open_sources(&home, source.as_deref())? {
                let stats = ragmonk_ml::embed_build(
                    &mut o.store,
                    &o.build,
                    embedder,
                    ragmonk_documents::chunker::EMBEDDING_TEXT_VERSION,
                )
                .map_err(|e| generic(format!("{}: {}", e.code, e.message)))?;
                let embedded = stats.inferred + stats.cache_reused;
                if embedded > 0 {
                    let dir = layout.project_dir(&project_id_for_canonical(&o.source.path));
                    ragmonk_ml::ann::sync(
                        &o.store,
                        &dir,
                        &o.build,
                        embedder.fingerprint(),
                        embedder.spec().dims,
                    )
                    .map_err(generic)?;
                }
                done.push(json!({"source_id": o.source.id, "embedded": embedded}));
            }
            lock.release();
            if j {
                return print_json(&json!({ "backfilled": done }));
            }
            if done.is_empty() {
                println!("No sources to backfill.");
            }
            for e in &done {
                let id = e["source_id"].as_str().unwrap_or_default();
                if e["embedded"].as_u64().unwrap_or(0) > 0 {
                    println!("{id}: embedded {} vector(s)", e["embedded"]);
                } else {
                    println!("{id}: nothing to backfill.");
                }
            }
        }
    }
    Ok(())
}
