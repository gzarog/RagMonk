//! Repository tooling for RagMonk, run as `cargo xtask <command>`.

mod audit;
mod branding;
mod package;

use std::path::PathBuf;

use anyhow::{bail, Result};

const USAGE: &str = "usage: cargo xtask <command>

commands:
  package --binary PATH --version X.Y.Z --target TRIPLE [--models DIR] --out DIR
      Build the release archive for one target and record it in
      DIR/SHA256SUMS.
  release-manifest --dist DIR
      Write DIR/SHA256SUMS over every release archive in DIR.
  branding-audit
      Fail if the product's former name appears in a tracked file.
  clean-slate-audit [--update-baseline] [--require-empty-baseline]
      Fail if a reference forbidden by ADR 0032 (clean slate)
      appears outside the narrow allowlist or above the shrink-only
      transition baseline (audit/clean-slate-baseline.tsv).";

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("package") => {
            let a = package::PackageArgs::parse(args)?;
            let archive = package::package(&audit::repo_root()?, &a)?;
            println!("{}", archive.display());
            Ok(())
        }
        Some("release-manifest") => {
            let dist = match (args.next().as_deref(), args.next()) {
                (Some("--dist"), Some(d)) => PathBuf::from(d),
                _ => bail!("usage: cargo xtask release-manifest --dist DIR"),
            };
            for name in package::release_manifest(&dist)? {
                println!("{name}");
            }
            Ok(())
        }
        Some("branding-audit") => branding::run(),
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
