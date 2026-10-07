//! Repository tooling for RagMonk, run as `cargo xtask <command>`.

mod audit;

use anyhow::{bail, Result};

const USAGE: &str = "usage: cargo xtask <command>

commands:
  clean-slate-audit [--update-baseline] [--require-empty-baseline]
      Fail if a reference forbidden by ADR 0032 (clean slate)
      appears outside the narrow allowlist or above the shrink-only
      transition baseline (audit/clean-slate-baseline.tsv).";

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("clean-slate-audit") => {
            let mut opts = audit::Options::default();
            for arg in args {
                match arg.as_str() {
                    "--update-baseline" => opts.update_baseline = true,
                    "--require-empty-baseline" => opts.require_empty_baseline = true,
                    other => bail!("unknown option {other}\n\n{USAGE}"),
                }
            }
            audit::run(&opts)
        }
        Some("-h" | "--help") | None => {
            println!("{USAGE}");
            Ok(())
        }
        Some(other) => bail!("unknown command {other}\n\n{USAGE}"),
    }
}
