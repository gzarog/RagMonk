//! `ragmonk` binary for the Rust rewrite.
//!
//! Implemented so far: `version` (RUST-00) and `config show|get|set`
//! (RUST-01). The JSON envelope matches the Python CLI's
//! `{"schema_version": "1", "data": {...}}`; `version --json` reports
//! `runtime: "rust"` instead of the Python interpreter version. Errors print
//! `Error: <redacted message>` to stderr and exit with the reference's
//! stable exit codes.

use std::process::ExitCode;

use clap::{Parser, Subcommand};
use ragmonk_config::loader::{dump_yaml, get_path, python_str, set_path};
use ragmonk_config::pyvalue::{py_repr_str, PyValue};
use ragmonk_config::{load_config, write_user_config, LoadOptions};
use ragmonk_core::errors::{RagMonkError, EXIT_GENERIC_FAILURE};
use ragmonk_core::paths::Home;
use ragmonk_core::version;
use ragmonk_telemetry::redact::redact_urls_in_text;

/// CLI JSON schema version, identical to `ragmonk.cli._common.SCHEMA_VERSION`.
const SCHEMA_VERSION: &str = "1";

#[derive(Parser)]
#[command(name = "ragmonk", about = "RagMonk (Rust rewrite preview)")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Show the RagMonk version.
    Version {
        #[arg(long = "json")]
        json: bool,
    },
    /// Inspect and edit configuration.
    #[command(subcommand)]
    Config(ConfigCommand),
    /// Prepare the Rust V2 control plane from a Python-era home.
    #[command(name = "migrate-to-rust-v2")]
    MigrateToRustV2(MigrateArgs),
}

#[derive(clap::Args)]
#[command(group(clap::ArgGroup::new("action").required(true).args(["check", "import_sources"])))]
struct MigrateArgs {
    /// Read-only report: preserved source/config definitions, ignored V1
    /// index-derived state and what an import would write.
    #[arg(long)]
    check: bool,
    /// Import V1 source definitions into the V2 control plane and mark
    /// every source for a full V2 rebuild. Never modifies or deletes V1 data.
    #[arg(long = "import-sources")]
    import_sources: bool,
    #[arg(long = "json")]
    json: bool,
}

#[derive(Subcommand)]
enum ConfigCommand {
    /// Print the effective configuration as YAML.
    Show,
    /// Print one value by dotted key, e.g. runtime.log_level.
    Get { key: String },
    /// Set one value by dotted key and write the user config.
    Set { key: String, value: String },
}

fn version_payload() -> serde_json::Value {
    serde_json::json!({
        "schema_version": SCHEMA_VERSION,
        "data": { "version": version::version(), "runtime": "rust" },
    })
}

/// Python `repr()` of a dumped config value (used for `config set`).
fn py_repr(value: &PyValue) -> String {
    match value {
        PyValue::Str(s) => py_repr_str(s),
        PyValue::List(items) => {
            let inner: Vec<String> = items.iter().map(py_repr).collect();
            format!("[{}]", inner.join(", "))
        }
        PyValue::Dict(entries) => {
            let inner: Vec<String> = entries
                .iter()
                .map(|(k, v)| format!("{}: {}", py_repr(k), py_repr(v)))
                .collect();
            format!("{{{}}}", inner.join(", "))
        }
        other => python_str(other).unwrap_or_default(),
    }
}

fn prepared_home() -> Result<Home, RagMonkError> {
    let home = Home::discover();
    home.ensure_layout().map_err(|e| {
        RagMonkError::new(
            ragmonk_core::ErrorKind::Generic,
            format!("cannot create {}: {e}", home.root().display()),
        )
    })?;
    Ok(home)
}

fn load(home: &Home) -> Result<ragmonk_config::RagMonkConfig, RagMonkError> {
    load_config(&LoadOptions {
        home: Some(home.root().to_path_buf()),
        ..LoadOptions::default()
    })
}

fn print_json(data: &impl serde::Serialize) -> Result<(), RagMonkError> {
    let envelope = serde_json::json!({ "schema_version": SCHEMA_VERSION, "data": data });
    let text = serde_json::to_string_pretty(&envelope)
        .map_err(|e| RagMonkError::new(ragmonk_core::ErrorKind::Generic, e.to_string()))?;
    println!("{text}");
    Ok(())
}

fn print_preflight(r: &ragmonk_storage::preflight::PreflightReport) {
    println!("RagMonk home: {}", r.home);
    println!(
        "Python V1 sources.db: {}",
        if r.v1_sources_db_present {
            "present"
        } else {
            "absent"
        }
    );
    println!(
        "Rust V2 control plane: {} ({})",
        r.v2_control_db,
        if r.v2_control_present {
            "present"
        } else {
            "will be created"
        }
    );
    println!("Sources ({}):", r.sources.len());
    for s in &r.sources {
        let ignored: i64 = s.ignored_v1_state.tables.iter().map(|(_, n)| n).sum();
        println!(
            "  {} {} [{}{}] -> {}; ignoring {} V1 index row(s)",
            s.id,
            s.path,
            if s.enabled { "enabled" } else { "disabled" },
            if s.path_exists { "" } else { ", path missing" },
            s.v2_action,
            ignored
        );
    }
    println!("Preserved:");
    for p in &r.preserved {
        println!("  - {p}");
    }
    println!("Ignored (never read by Rust V2):");
    for p in &r.ignored {
        println!("  - {p}");
    }
    println!("An import would write:");
    for w in &r.writes {
        println!("  - {w}");
    }
    println!("Nothing is deleted; Python V1 files are left untouched.");
}

fn run(cli: Cli) -> Result<(), RagMonkError> {
    match cli.command {
        Command::Version { json } => {
            if json {
                let text = serde_json::to_string_pretty(&version_payload()).map_err(|e| {
                    RagMonkError::new(ragmonk_core::ErrorKind::Generic, e.to_string())
                })?;
                println!("{text}");
            } else {
                println!("ragmonk {}", version::version());
            }
        }
        Command::Config(ConfigCommand::Show) => {
            let home = prepared_home()?;
            println!("{}", dump_yaml(&load(&home)?));
        }
        Command::Config(ConfigCommand::Get { key }) => {
            let home = prepared_home()?;
            let value = get_path(&load(&home)?, &key)?;
            println!("{}", python_str(&value).unwrap_or_else(|| py_repr(&value)));
        }
        Command::Config(ConfigCommand::Set { key, value }) => {
            let home = prepared_home()?;
            let (updated, stored) = set_path(&load(&home)?, &key, &value)?;
            let path = write_user_config(&updated, &home)
                .map_err(|e| RagMonkError::new(ragmonk_core::ErrorKind::Generic, e.to_string()))?;
            println!("Set {key} = {} in {}", py_repr(&stored), path.display());
        }
        Command::MigrateToRustV2(args) => {
            let home = Home::discover();
            if args.check {
                let report = ragmonk_storage::preflight::preflight(&home)?;
                if args.json {
                    print_json(&report)?;
                } else {
                    print_preflight(&report);
                }
            } else {
                let home = prepared_home()?;
                let cache = load(&home)?.runtime.sqlite_cache_size_mb;
                let result = ragmonk_storage::preflight::import_sources(&home, cache)?;
                if args.json {
                    print_json(&result)?;
                } else {
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
                }
            }
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    // clap exits with code 2 on usage errors, matching EXIT_INVALID_ARGUMENTS.
    let cli = Cli::parse();
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("Error: {}", redact_urls_in_text(err.message()));
            ExitCode::from(if err.exit_code() == 0 {
                EXIT_GENERIC_FAILURE
            } else {
                err.exit_code()
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_json_uses_python_envelope() {
        let value = version_payload();
        assert_eq!(value["schema_version"], "1");
        assert_eq!(value["data"]["version"], version::version());
    }

    #[test]
    fn cli_definition_is_valid() {
        use clap::CommandFactory;
        Cli::command().debug_assert();
    }
}
