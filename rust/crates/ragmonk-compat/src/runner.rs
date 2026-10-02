//! Executes a manifest against one implementation in isolated homes.

use std::collections::BTreeMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{bail, Context};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::canon::{canonical_json_with, canonical_text, IdMode, Masks};
use crate::manifest::{Manifest, OutputKind, Scenario, Step};
use crate::sqlite_inventory;

/// File proving a directory was created by this harness, so it may be wiped.
const WORKDIR_MARKER: &str = ".ragmonk-compat-workdir";

/// How to launch one implementation, e.g. `ragmonk` from a Python venv or
/// the Rust `target/debug/ragmonk`.
#[derive(Debug, Clone)]
pub struct Implementation {
    pub name: String,
    pub program: PathBuf,
    pub prefix_args: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct RunOptions {
    pub repo_root: PathBuf,
    /// Parent of the per-scenario work directories. Use the same value for
    /// both implementations so path-derived IDs are comparable in strict mode.
    pub work_root: PathBuf,
    pub id_mode: IdMode,
    pub step_timeout: Duration,
    /// Only run these scenario ids (all when empty).
    pub only: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CaptureFile {
    pub manifest_version: u32,
    pub reference_commit: String,
    pub implementation: String,
    pub id_mode: IdMode,
    pub scenarios: Vec<ScenarioCapture>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ScenarioCapture {
    pub id: String,
    pub steps: Vec<StepCapture>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StepCapture {
    pub id: String,
    pub exit_code: Option<i32>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub timed_out: bool,
    pub stdout: Value,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub stderr: Vec<String>,
}

struct RawStep {
    exit_code: Option<i32>,
    timed_out: bool,
    stdout: String,
    stderr: String,
}

pub fn run_manifest(
    manifest: &Manifest,
    imp: &Implementation,
    opts: &RunOptions,
) -> anyhow::Result<CaptureFile> {
    let mut scenarios = Vec::new();
    for scenario in &manifest.scenarios {
        if !opts.only.is_empty() && !opts.only.contains(&scenario.id) {
            continue;
        }
        scenarios.push(
            run_scenario(manifest, scenario, imp, opts)
                .with_context(|| format!("scenario {}", scenario.id))?,
        );
    }
    Ok(CaptureFile {
        manifest_version: manifest.manifest_version,
        reference_commit: manifest.reference_commit.clone(),
        implementation: imp.name.clone(),
        id_mode: opts.id_mode,
        scenarios,
    })
}

/// Creates (or safely resets) `<work_root>/<scenario>`; refuses to delete a
/// pre-existing directory the harness did not create.
pub fn prepare_workdir(work_root: &Path, scenario_id: &str) -> anyhow::Result<PathBuf> {
    let dir = work_root.join(scenario_id);
    if dir.exists() {
        if !dir.join(WORKDIR_MARKER).is_file() {
            bail!(
                "refusing to reset {}: it exists but lacks {WORKDIR_MARKER}",
                dir.display()
            );
        }
        std::fs::remove_dir_all(&dir).with_context(|| format!("reset {}", dir.display()))?;
    }
    std::fs::create_dir_all(&dir)?;
    std::fs::write(dir.join(WORKDIR_MARKER), b"")?;
    Ok(dir)
}

fn copy_recursive(from: &Path, to: &Path) -> anyhow::Result<()> {
    let meta = std::fs::symlink_metadata(from).with_context(|| format!("{}", from.display()))?;
    if meta.is_dir() {
        std::fs::create_dir_all(to)?;
        let mut entries: Vec<_> = std::fs::read_dir(from)?.collect::<Result<_, _>>()?;
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            copy_recursive(&entry.path(), &to.join(entry.file_name()))?;
        }
    } else if meta.is_file() {
        if let Some(parent) = to.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::copy(from, to)?;
    }
    // Symlinks are skipped: fixtures must be plain files.
    Ok(())
}

fn run_scenario(
    manifest: &Manifest,
    scenario: &Scenario,
    imp: &Implementation,
    opts: &RunOptions,
) -> anyhow::Result<ScenarioCapture> {
    std::fs::create_dir_all(&opts.work_root)?;
    let work = prepare_workdir(&opts.work_root, &scenario.id)?;
    let home = work.join("home");
    let source = work.join("source");
    std::fs::create_dir_all(&source)?;
    for fixture in &scenario.fixtures {
        copy_recursive(
            &opts.repo_root.join(&fixture.from),
            &source.join(&fixture.to),
        )?;
    }

    let mut vars: BTreeMap<String, String> = BTreeMap::new();
    let mut raws: Vec<(&Step, RawStep)> = Vec::new();
    for step in &scenario.steps {
        let raw = if step.sqlite_inventory {
            RawStep {
                exit_code: Some(0),
                timed_out: false,
                stdout: String::new(),
                stderr: String::new(),
            }
        } else {
            let args: Vec<String> = step
                .args
                .iter()
                .map(|a| substitute(a, &work, &home, &source, &vars))
                .collect();
            let raw = run_command(imp, &args, &home, &manifest.env, opts.step_timeout)
                .with_context(|| format!("step {}", step.id))?;
            if let Some(capture) = &step.capture {
                let re = regex::Regex::new(&capture.regex)?;
                if let Some(value) = re
                    .captures(&raw.stdout)
                    .and_then(|c| c.get(1))
                    .map(|m| m.as_str().to_owned())
                {
                    vars.insert(capture.name.clone(), value);
                }
            }
            raw
        };
        raws.push((step, raw));
    }

    let masks = build_masks(&work, &home, &vars, opts.id_mode);
    let project_names = project_dir_names(&home);
    let rename = |segment: &str| -> String {
        if opts.id_mode == IdMode::Portable && project_names.iter().any(|p| p == segment) {
            "<PROJECT>".into()
        } else {
            segment.into()
        }
    };

    let mut steps = Vec::new();
    for (step, raw) in raws {
        let stdout = if step.sqlite_inventory {
            canonical_json_with(
                &sqlite_inventory::inventory(&home, &rename)?,
                &masks,
                &step.volatile_keys,
                &step.unordered_arrays,
            )
        } else {
            canonical_stdout(step, &raw.stdout, &masks)
        };
        steps.push(StepCapture {
            id: step.id.clone(),
            exit_code: raw.exit_code,
            timed_out: raw.timed_out,
            stdout,
            stderr: canonical_text(&raw.stderr, &masks),
        });
    }
    Ok(ScenarioCapture {
        id: scenario.id.clone(),
        steps,
    })
}

fn canonical_stdout(step: &Step, stdout: &str, masks: &Masks) -> Value {
    let parsed = match step.output {
        OutputKind::None => return Value::Null,
        OutputKind::Text => None,
        OutputKind::Json => serde_json::from_str::<Value>(stdout).ok(),
        OutputKind::Yaml => serde_yaml::from_str::<Value>(stdout).ok(),
    };
    match parsed {
        Some(value) => {
            canonical_json_with(&value, masks, &step.volatile_keys, &step.unordered_arrays)
        }
        // Unparseable structured output is recorded as text so a diff shows it.
        None => Value::Array(
            canonical_text(stdout, masks)
                .into_iter()
                .map(Value::String)
                .collect(),
        ),
    }
}

fn build_masks(work: &Path, home: &Path, vars: &BTreeMap<String, String>, mode: IdMode) -> Masks {
    let mut masks = Masks::default();
    for (path, label) in [(home, "<HOME>"), (work, "<WORK>")] {
        masks.add(path.to_string_lossy(), label);
        if let Ok(real) = std::fs::canonicalize(path) {
            masks.add(real.to_string_lossy(), label);
        }
    }
    if mode == IdMode::Portable {
        for (name, value) in vars {
            masks.add(value.clone(), format!("<VAR:{name}>"));
        }
        for project in project_dir_names(home) {
            masks.add(project, "<PROJECT>");
        }
    }
    masks
}

fn project_dir_names(home: &Path) -> Vec<String> {
    let Ok(entries) = std::fs::read_dir(home.join("projects")) else {
        return Vec::new();
    };
    entries
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect()
}

fn substitute(
    arg: &str,
    work: &Path,
    home: &Path,
    source: &Path,
    vars: &BTreeMap<String, String>,
) -> String {
    let mut out = arg
        .replace("{work}", &work.to_string_lossy())
        .replace("{home}", &home.to_string_lossy())
        .replace("{source}", &source.to_string_lossy());
    for (name, value) in vars {
        out = out.replace(&format!("{{{name}}}"), value);
    }
    out
}

fn run_command(
    imp: &Implementation,
    args: &[String],
    home: &Path,
    extra_env: &[(String, String)],
    timeout: Duration,
) -> anyhow::Result<RawStep> {
    let mut cmd = Command::new(&imp.program);
    cmd.args(&imp.prefix_args).args(args);
    // Isolation: never inherit a user's RagMonk settings or credentials.
    for (key, _) in std::env::vars_os() {
        let key = key.to_string_lossy().into_owned();
        if key.starts_with("RAGMONK_") {
            cmd.env_remove(key);
        }
    }
    cmd.env("RAGMONK_HOME", home)
        .env("RAGMONK_UPDATES__ENABLED", "false")
        .env("NO_COLOR", "1")
        .env("TERM", "dumb")
        .env("COLUMNS", "200")
        .env("PYTHONHASHSEED", "0")
        .env("PYTHONUTF8", "1");
    for (key, value) in extra_env {
        cmd.env(key, value);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd
        .spawn()
        .with_context(|| format!("spawn {}", imp.program.display()))?;

    // One reader thread per pipe (bounded: exactly two per step) so a chatty
    // child can never deadlock on a full pipe buffer.
    let mut out_pipe = child.stdout.take().context("stdout pipe")?;
    let mut err_pipe = child.stderr.take().context("stderr pipe")?;
    let out_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = out_pipe.read_to_end(&mut buf);
        buf
    });
    let err_reader = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = err_pipe.read_to_end(&mut buf);
        buf
    });

    let started = Instant::now();
    let mut timed_out = false;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if started.elapsed() >= timeout {
            timed_out = true;
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    let stdout = out_reader.join().unwrap_or_default();
    let stderr = err_reader.join().unwrap_or_default();
    Ok(RawStep {
        exit_code: status.and_then(|s| s.code()),
        timed_out,
        stdout: String::from_utf8_lossy(&stdout).into_owned(),
        stderr: String::from_utf8_lossy(&stderr).into_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refuses_to_reset_foreign_directory() {
        let root = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(root.path().join("s")).unwrap();
        std::fs::write(root.path().join("s").join("precious"), b"x").unwrap();
        assert!(prepare_workdir(root.path(), "s").is_err());
        assert!(root.path().join("s").join("precious").exists());
    }

    #[test]
    fn resets_own_directory() {
        let root = tempfile::tempdir().unwrap();
        let dir = prepare_workdir(root.path(), "s").unwrap();
        std::fs::write(dir.join("stale"), b"x").unwrap();
        let dir = prepare_workdir(root.path(), "s").unwrap();
        assert!(!dir.join("stale").exists());
    }

    #[test]
    fn substitutes_placeholders_and_vars() {
        let mut vars = BTreeMap::new();
        vars.insert("source_id".to_string(), "src_1".to_string());
        let s = substitute(
            "{source}|{source_id}|{home}",
            Path::new("/w"),
            Path::new("/w/home"),
            Path::new("/w/source"),
            &vars,
        );
        assert_eq!(s, "/w/source|src_1|/w/home");
    }
}
