//! Minimal `ragmonk` binary for the Rust rewrite (plan phase RUST-00).
//!
//! Only `version` exists so far. The JSON envelope matches the Python CLI's
//! `{"schema_version": "1", "data": {...}}` contract; the `data` payload
//! reports `runtime: "rust"` instead of the Python interpreter version and
//! is explicitly *not* a parity claim -- later phases port each command
//! behind the compat harness.

use std::process::ExitCode;

use clap::{Parser, Subcommand};

/// CLI JSON schema version, identical to `ragmonk.cli._common.SCHEMA_VERSION`.
const SCHEMA_VERSION: &str = "1";
const VERSION: &str = env!("CARGO_PKG_VERSION");

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
}

fn version_payload() -> serde_json::Value {
    serde_json::json!({
        "schema_version": SCHEMA_VERSION,
        "data": { "version": VERSION, "runtime": "rust" },
    })
}

fn main() -> ExitCode {
    // clap exits with code 2 on usage errors, matching EXIT_INVALID_ARGUMENTS.
    let cli = Cli::parse();
    match cli.command {
        Command::Version { json } => {
            if json {
                match serde_json::to_string_pretty(&version_payload()) {
                    Ok(text) => println!("{text}"),
                    Err(_) => return ExitCode::from(1),
                }
            } else {
                println!("ragmonk {VERSION}");
            }
        }
    }
    ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_json_uses_python_envelope() {
        let value = version_payload();
        assert_eq!(value["schema_version"], "1");
        assert_eq!(value["data"]["version"], VERSION);
    }

    #[test]
    fn cli_definition_is_valid() {
        use clap::CommandFactory;
        Cli::command().debug_assert();
    }
}
