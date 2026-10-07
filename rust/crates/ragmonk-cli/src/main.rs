//! `ragmonk` binary for the Rust rewrite.
//!
//! Implemented so far: `version` (RUST-00) and `config show|get|set`
//! (RUST-01), `daemon start|stop|restart|status|run` (RUST-11), `init`, `source …`,
//! `index`, `status`, `docs` and `watch` (RUST-12). The JSON envelope matches the Python CLI's
//! `{"schema_version": "1", "data": {...}}`; `version --json` reports
//! `runtime: "rust"` instead of the Python interpreter version. Errors print
//! `Error: <redacted message>` to stderr and exit with the reference's
//! stable exit codes.

mod ai_cmd;
mod daemon_cmd;
mod doctor_cmd;
mod mcp;
mod ops_cmd;
mod query_cmd;
mod status_cmd;
mod ui;
mod update_cmd;
mod workflow;

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
    /// Rust V2 OpenSearch/Elasticsearch schema.
    #[command(name = "server-v2", subcommand)]
    ServerV2(ServerV2Command),
    /// Manage the background indexing daemon.
    #[command(subcommand)]
    Daemon(daemon_cmd::DaemonCommand),
    /// Bootstrap the RagMonk runtime directory.
    Init(workflow::InitArgs),
    /// Manage registered sources.
    #[command(subcommand)]
    Source(workflow::SourceCommand),
    /// Scan sources and process pending files.
    Index {
        /// Only index this source id.
        #[arg(long = "source")]
        source: Option<String>,
    },
    /// Show indexing status.
    Status(status_cmd::StatusArgs),
    /// List indexed documents.
    Docs {
        /// Only list this source id.
        #[arg(long = "source")]
        source: Option<String>,
        #[arg(long = "json")]
        json: bool,
    },
    /// Run the indexing daemon in the foreground.
    Watch,
    /// Answer a question with the configured AI provider, grounded in
    /// the evidence `explore` retrieves.
    Ask {
        /// Natural-language question to ask.
        question: String,
        #[arg(long = "json")]
        json: bool,
    },
    /// Start the local administration web interface.
    Ui {
        /// Interface to bind. Defaults to localhost.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        /// Port to listen on.
        #[arg(long, default_value_t = 8765)]
        port: u16,
        /// Do not open a browser automatically.
        #[arg(long = "no-browser")]
        no_browser: bool,
    },
    /// Manage subscription AI providers.
    #[command(subcommand)]
    Ai(ai_cmd::AiCommand),
    /// Serve the indexed knowledge to agents (MCP over stdio).
    Serve {
        /// Run the MCP server over stdio.
        #[arg(long = "mcp")]
        mcp: bool,
    },
    /// Lexical search across code and documents.
    Search(query_cmd::SearchArgs),
    /// Look up a code symbol by name.
    Symbol {
        /// Symbol name or fully qualified name.
        name: String,
        #[arg(long = "json")]
        json: bool,
    },
    /// Show entities that call the given symbol.
    Callers(query_cmd::GraphArgs),
    /// Show entities the given symbol calls.
    Callees(query_cmd::GraphArgs),
    /// Show all edges touching the given symbol.
    References(query_cmd::GraphArgs),
    /// Show blast-radius impact analysis for a symbol.
    Impact(query_cmd::GraphArgs),
    /// Explore code and documents for a question or identifier.
    Explore {
        /// Natural-language or identifier query.
        query: String,
        #[arg(long = "json")]
        json: bool,
    },
    /// Inspect and manually correct the link graph.
    #[command(subcommand)]
    Link(query_cmd::LinkCommand),
    /// Run health checks.
    Doctor {
        #[arg(long = "json")]
        json: bool,
    },
    /// Show a condensed health summary.
    Health {
        #[arg(long = "json")]
        json: bool,
    },
    /// Create a restorable backup archive of RagMonk's databases.
    Backup {
        /// Output archive path (default: <home>/backups/ragmonk-backup-<timestamp>.tar.gz).
        dest: Option<String>,
        #[arg(long = "json")]
        json: bool,
    },
    /// Restore RagMonk's databases from a backup archive.
    Restore {
        /// Path to a backup archive created by 'ragmonk backup'.
        archive: String,
        #[arg(long = "json")]
        json: bool,
    },
    /// Re-index one or every source's derived knowledge from scratch.
    Rebuild {
        /// Only rebuild this source id.
        #[arg(long = "source")]
        source: Option<String>,
        /// Check every source root first and ask for confirmation.
        #[arg(long)]
        fresh: bool,
        /// Skip the confirmation prompt for --fresh.
        #[arg(long)]
        yes: bool,
        #[arg(long = "json")]
        json: bool,
    },
    /// Check for and install RagMonk updates.
    Update {
        #[command(subcommand)]
        command: Option<update_cmd::UpdateCommand>,
    },
    /// Apply pending schema migrations, backing up first if needed.
    Upgrade {
        #[arg(long = "json")]
        json: bool,
    },
    /// Remove RagMonk's data (and explain how to remove the binary).
    Uninstall {
        /// Remove only the application; keep RAGMONK_HOME.
        #[arg(long = "keep-data")]
        keep_data: bool,
        /// Skip the confirmation prompt.
        #[arg(long, short = 'y')]
        yes: bool,
        #[arg(long = "json")]
        json: bool,
    },
    /// Manage the semantic-search ANN index.
    #[command(subcommand)]
    Vectors(ops_cmd::VectorsCommand),
}

#[derive(Subcommand)]
enum ServerV2Command {
    /// Print the V2 index schema manifest for the configured engine.
    Schema {
        #[arg(long = "json")]
        json: bool,
    },
    /// Create any missing V2 indexes and verify existing ones (never modifies them).
    Init {
        #[arg(long = "json")]
        json: bool,
    },
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

/// Console width as Rich computes it for a non-terminal: `COLUMNS` when
/// set, else 80.
fn console_width() -> usize {
    std::env::var("COLUMNS")
        .ok()
        .and_then(|c| c.trim().parse::<usize>().ok())
        .filter(|w| *w > 0)
        .unwrap_or(80)
}

/// `rich.pretty` layout, as `console.print(value)` renders a dict or list.
/// A container is expanded (one child per line, 4-space indent) only when
/// its one-line form, plus indent and trailing separator, exceeds `width`.
/// The decision is made per node, as in Rich's `_Line.check_length`.
fn rich_pretty(value: &PyValue, width: usize) -> String {
    /// `(open, close, [(key repr, value)])` for a non-empty container.
    type Kids<'a> = (char, char, Vec<(Option<String>, &'a PyValue)>);
    fn children(v: &PyValue) -> Option<Kids<'_>> {
        match v {
            PyValue::Dict(e) if !e.is_empty() => Some((
                '{',
                '}',
                e.iter().map(|(k, v)| (Some(py_repr(k)), v)).collect(),
            )),
            PyValue::List(items) if !items.is_empty() => {
                Some(('[', ']', items.iter().map(|v| (None, v)).collect()))
            }
            _ => None,
        }
    }
    fn render(
        key: Option<&str>,
        v: &PyValue,
        ws: &str,
        suffix: &str,
        last: bool,
        width: usize,
        out: &mut Vec<String>,
    ) {
        let prefix = key.map(|k| format!("{k}: ")).unwrap_or_default();
        let one_line = format!("{prefix}{}", py_repr(v));
        let tail = if last { "" } else { suffix.trim_end() };
        let fits = ws.chars().count() + suffix.chars().count() + one_line.chars().count() <= width;
        match children(v) {
            Some((open, close, kids)) if !fits => {
                out.push(format!("{ws}{prefix}{open}"));
                let inner = format!("{ws}    ");
                let n = kids.len();
                for (i, (k, child)) in kids.into_iter().enumerate() {
                    render(k.as_deref(), child, &inner, ", ", i + 1 == n, width, out);
                }
                out.push(format!("{ws}{close}{tail}"));
            }
            _ => out.push(format!("{ws}{one_line}{tail}")),
        }
    }
    let mut out = Vec::new();
    render(None, value, "", "", true, width, &mut out);
    out.join("\n")
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

fn server_config() -> Result<ragmonk_config::model::ServerStorageConfig, RagMonkError> {
    let home = prepared_home()?;
    let cfg = load(&home)?;
    if cfg.storage.mode != "server" {
        return Err(RagMonkError::config(
            "storage.mode is not 'server'; configure storage.server first",
        ));
    }
    Ok(cfg.storage.server)
}

fn run_server_v2(cmd: ServerV2Command) -> Result<(), RagMonkError> {
    use ragmonk_backends::engine::{default_vector_spec, Engine};
    use ragmonk_backends::{schema, ServerBackend};
    match cmd {
        ServerV2Command::Schema { json } => {
            let home = prepared_home()?;
            let server = load(&home)?.storage.server;
            let engine = if server.engine.as_str() == "elasticsearch" {
                Engine::Elasticsearch
            } else {
                Engine::OpenSearch
            };
            let manifest = schema::manifest(
                &schema::v2_prefix(&server.index_prefix),
                engine,
                Some(&default_vector_spec()),
            );
            if json {
                print_json(&manifest)?;
            } else {
                for idx in manifest["indexes"].as_array().into_iter().flatten() {
                    println!("{}", idx["name"].as_str().unwrap_or_default());
                }
            }
        }
        ServerV2Command::Init { json } => {
            let backend = ServerBackend::connect(&server_config()?, Some(default_vector_spec()))?;
            let report = backend.init()?;
            if json {
                print_json(&report)?;
            } else {
                println!(
                    "{} V2 index(es) created, {} already present and verified (prefix {}).",
                    report.created.len(),
                    report.existing.len(),
                    backend.prefix()
                );
            }
        }
    }
    Ok(())
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
            let text = match &value {
                PyValue::Dict(_) | PyValue::List(_) => rich_pretty(&value, console_width()),
                other => python_str(other).unwrap_or_else(|| py_repr(other)),
            };
            println!("{text}");
        }
        Command::Config(ConfigCommand::Set { key, value }) => {
            let home = prepared_home()?;
            let (updated, stored) = set_path(&load(&home)?, &key, &value)?;
            let path = write_user_config(&updated, &home)
                .map_err(|e| RagMonkError::new(ragmonk_core::ErrorKind::Generic, e.to_string()))?;
            println!("Set {key} = {} in {}", py_repr(&stored), path.display());
        }
        Command::ServerV2(cmd) => run_server_v2(cmd)?,
        Command::Daemon(cmd) => daemon_cmd::run(cmd)?,
        Command::Init(a) => workflow::init(&a)?,
        Command::Source(cmd) => workflow::source(cmd)?,
        Command::Index { source } => workflow::index(source)?,
        Command::Status(a) => status_cmd::status(&a)?,
        Command::Docs { source, json } => workflow::docs(source, json)?,
        Command::Watch => daemon_cmd::run(daemon_cmd::DaemonCommand::Run)?,
        Command::Serve { mcp } => mcp::serve(mcp)?,
        Command::Ask { question, json } => ai_cmd::ask(&question, json)?,
        Command::Ai(cmd) => ai_cmd::run(cmd)?,
        Command::Ui {
            host,
            port,
            no_browser,
        } => ui::serve(&host, port, no_browser)?,
        Command::Search(a) => query_cmd::search(&a)?,
        Command::Symbol { name, json } => query_cmd::symbol(&name, json)?,
        Command::Callers(a) => query_cmd::calls(&a, ragmonk_retrieval::graph::Direction::Incoming)?,
        Command::Callees(a) => query_cmd::calls(&a, ragmonk_retrieval::graph::Direction::Outgoing)?,
        Command::References(a) => query_cmd::references(&a)?,
        Command::Impact(a) => query_cmd::impact(&a)?,
        Command::Explore { query, json } => query_cmd::explore(&query, json)?,
        Command::Link(cmd) => query_cmd::link(cmd)?,
        Command::Doctor { json } => doctor_cmd::doctor(json)?,
        Command::Health { json } => doctor_cmd::health(json)?,
        Command::Backup { dest, json } => ops_cmd::backup(dest, json)?,
        Command::Restore { archive, json } => ops_cmd::restore(&archive, json)?,
        Command::Rebuild {
            source,
            fresh,
            yes,
            json,
        } => ops_cmd::rebuild(source, fresh, yes, json)?,
        Command::Update { command } => update_cmd::run(command)?,
        Command::Upgrade { json } => ops_cmd::upgrade(json)?,
        Command::Uninstall {
            keep_data,
            yes,
            json,
        } => ops_cmd::uninstall(keep_data, yes, json)?,
        Command::Vectors(cmd) => ops_cmd::vectors(cmd)?,
    }
    Ok(())
}

fn main() -> ExitCode {
    // clap exits with code 2 on usage errors, matching EXIT_INVALID_ARGUMENTS.
    let cli = Cli::parse();
    // Commands that must stay quiet and network-free: the update commands
    // themselves, the MCP/daemon processes, and the ones `update install`
    // runs on a freshly installed binary.
    if std::env::var_os("RAGMONK_NO_UPDATE_CHECK").is_none()
        && !matches!(
            cli.command,
            Command::Update { .. }
                | Command::Version { .. }
                | Command::Serve { .. }
                | Command::Daemon(_)
                | Command::Watch
                | Command::Upgrade { .. }
                | Command::Doctor { .. }
        )
    {
        update_cmd::on_startup();
    }
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            // An empty message is a deliberate silent exit (e.g. doctor's
            // UNHEALTHY verdict, already printed).
            if !err.message().is_empty() {
                eprintln!("Error: {}", redact_urls_in_text(err.message()));
            }
            ExitCode::from(if err.exit_code() == 0 {
                EXIT_GENERIC_FAILURE
            } else {
                err.exit_code()
            })
        }
    }
}

#[cfg(test)]
mod pretty_tests {
    use super::*;

    #[test]
    fn expands_only_what_overflows() {
        let cfg = ragmonk_config::RagMonkConfig::default();
        let v = get_path(&cfg, "storage").unwrap();
        let want = "{\n    'mode': 'local',\n    'server': {\n        'engine': 'opensearch',\n        'url': '',\n        'index_prefix': 'ragmonk',\n        'verify_tls': True,\n        'request_timeout_seconds': 30.0,\n        'bulk': {'max_actions': 500, 'max_bytes': 5000000, 'concurrency': 2, 'max_retries': 3}\n    }\n}";
        assert_eq!(rich_pretty(&v, 200), want);
        assert_eq!(rich_pretty(&v, 400).lines().count(), 1);
        assert_eq!(rich_pretty(&PyValue::List(vec![]), 10), "[]");
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
