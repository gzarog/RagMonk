//! `ragmonk-compat` -- capture/compare canonical RagMonk behavior.
//!
//! ```text
//! ragmonk-compat capture   --impl python --program .venv/bin/ragmonk --out py.json
//! ragmonk-compat stability --impl python --program .venv/bin/ragmonk --runs 2
//! ragmonk-compat compare   py.json rust.json
//! ```

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use anyhow::Context;
use clap::{Args, Parser, Subcommand};
use ragmonk_compat::canon::IdMode;
use ragmonk_compat::compare::{diff, gate};
use ragmonk_compat::manifest::Manifest;
use ragmonk_compat::runner::{run_manifest, CaptureFile, Implementation, RunOptions};

#[derive(Parser)]
#[command(
    name = "ragmonk-compat",
    about = "RagMonk Python-vs-Rust differential harness"
)]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Args)]
struct RunArgs {
    /// Implementation label recorded in the capture (e.g. python, rust).
    #[arg(long = "impl")]
    implementation: String,
    /// Executable to run, e.g. the Python venv's `ragmonk` or `target/debug/ragmonk`.
    #[arg(long)]
    program: PathBuf,
    /// Arguments inserted before every step's arguments.
    #[arg(long = "prefix-arg", allow_hyphen_values = true)]
    prefix_args: Vec<String>,
    #[arg(long, default_value = "rust/compat/manifest.json")]
    manifest: PathBuf,
    /// Repository root fixture paths are resolved against.
    #[arg(long, default_value = ".")]
    repo_root: PathBuf,
    /// Parent directory for isolated per-scenario work dirs.
    #[arg(long)]
    work_root: Option<PathBuf>,
    /// Keep path-derived IDs (same-machine Python-vs-Rust comparisons).
    #[arg(long)]
    strict_ids: bool,
    #[arg(long, default_value_t = 600)]
    step_timeout_secs: u64,
    /// Restrict to these scenario ids.
    #[arg(long = "scenario")]
    only: Vec<String>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the manifest against one implementation and write a canonical capture.
    Capture {
        #[command(flatten)]
        run: RunArgs,
        #[arg(long)]
        out: PathBuf,
    },
    /// Capture N times and prove the canonical output is identical.
    Stability {
        #[command(flatten)]
        run: RunArgs,
        #[arg(long, default_value_t = 2)]
        runs: u32,
        /// Optionally write the first capture here.
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// Gate a candidate capture on the steps owned by `--phase` or earlier.
    Gate {
        reference: PathBuf,
        candidate: PathBuf,
        #[arg(long)]
        phase: String,
        #[arg(long, default_value = "rust/compat/manifest.json")]
        manifest: PathBuf,
        #[arg(long, default_value_t = 1e-6)]
        float_tolerance: f64,
    },
    /// Diff two canonical captures; exits 1 when they differ.
    Compare {
        left: PathBuf,
        right: PathBuf,
        #[arg(long, default_value_t = 1e-6)]
        float_tolerance: f64,
        /// Also compare the `implementation` label (ignored by default).
        #[arg(long)]
        keep_label: bool,
    },
}

fn options(run: &RunArgs) -> anyhow::Result<(Manifest, Implementation, RunOptions)> {
    let manifest = Manifest::load(&run.manifest)?;
    let imp = Implementation {
        name: run.implementation.clone(),
        program: run.program.clone(),
        prefix_args: run.prefix_args.clone(),
    };
    let opts = RunOptions {
        repo_root: run.repo_root.clone(),
        work_root: run
            .work_root
            .clone()
            .unwrap_or_else(|| std::env::temp_dir().join("ragmonk-compat")),
        id_mode: if run.strict_ids {
            IdMode::Strict
        } else {
            IdMode::Portable
        },
        step_timeout: Duration::from_secs(run.step_timeout_secs),
        only: run.only.clone(),
    };
    Ok((manifest, imp, opts))
}

fn write_capture(path: &PathBuf, capture: &CaptureFile) -> anyhow::Result<()> {
    let mut text = serde_json::to_string_pretty(capture)?;
    text.push('\n');
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, text).with_context(|| format!("write {}", path.display()))
}

fn load_value(path: &PathBuf) -> anyhow::Result<serde_json::Value> {
    let text = std::fs::read_to_string(path).with_context(|| format!("read {}", path.display()))?;
    Ok(serde_json::from_str(&text)?)
}

fn report(diffs: &[ragmonk_compat::compare::Difference]) -> ExitCode {
    if diffs.is_empty() {
        println!("IDENTICAL: no canonical differences");
        return ExitCode::SUCCESS;
    }
    println!("DIFFERENT: {} canonical difference(s)", diffs.len());
    for d in diffs {
        println!(
            "  {}\n    left:  {}\n    right: {}",
            d.path, d.left, d.right
        );
    }
    ExitCode::from(1)
}

fn run() -> anyhow::Result<ExitCode> {
    match Cli::parse().command {
        Cmd::Capture { run, out } => {
            let (manifest, imp, opts) = options(&run)?;
            let capture = run_manifest(&manifest, &imp, &opts)?;
            write_capture(&out, &capture)?;
            println!("wrote {}", out.display());
            Ok(ExitCode::SUCCESS)
        }
        Cmd::Stability { run, runs, out } => {
            anyhow::ensure!(runs >= 2, "--runs must be at least 2");
            let (manifest, imp, opts) = options(&run)?;
            let first = run_manifest(&manifest, &imp, &opts)?;
            let first_value = serde_json::to_value(&first)?;
            let mut all = Vec::new();
            for i in 1..runs {
                let next = serde_json::to_value(run_manifest(&manifest, &imp, &opts)?)?;
                let diffs = diff(&first_value, &next, 0.0);
                println!("run 1 vs run {}: {} difference(s)", i + 1, diffs.len());
                all.extend(diffs);
            }
            if let Some(out) = out {
                write_capture(&out, &first)?;
            }
            Ok(report(&all))
        }
        Cmd::Gate {
            reference,
            candidate,
            phase,
            manifest,
            float_tolerance,
        } => {
            let manifest = Manifest::load(&manifest)?;
            let number = ragmonk_compat::manifest::phase_number(&phase)
                .with_context(|| format!("invalid phase {phase:?}"))?;
            let (gated, diffs) = gate(
                &manifest,
                &load_value(&reference)?,
                &load_value(&candidate)?,
                number,
                float_tolerance,
            )
            .map_err(anyhow::Error::msg)?;
            anyhow::ensure!(
                !gated.is_empty(),
                "no manifest steps are owned by {phase} or earlier"
            );
            println!("gated steps ({}): {}", gated.len(), gated.join(", "));
            Ok(report(&diffs))
        }
        Cmd::Compare {
            left,
            right,
            float_tolerance,
            keep_label,
        } => {
            let mut a = load_value(&left)?;
            let mut b = load_value(&right)?;
            if !keep_label {
                for v in [&mut a, &mut b] {
                    if let Some(map) = v.as_object_mut() {
                        map.remove("implementation");
                    }
                }
            }
            Ok(report(&diff(&a, &b, float_tolerance)))
        }
    }
}

fn main() -> ExitCode {
    match run() {
        Ok(code) => code,
        Err(err) => {
            eprintln!("error: {err:#}");
            ExitCode::from(2)
        }
    }
}
