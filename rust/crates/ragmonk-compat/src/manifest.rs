//! Compatibility fixture manifest (`rust/compat/manifest.json`).

use std::path::Path;

use serde::{Deserialize, Serialize};

/// Bumped whenever the manifest or capture format changes shape.
pub const MANIFEST_VERSION: u32 = 1;

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub manifest_version: u32,
    /// Python commit the committed baseline was captured from.
    pub reference_commit: String,
    /// Extra environment applied to every step (on top of the isolation env).
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub scenarios: Vec<Scenario>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Scenario {
    pub id: String,
    #[serde(default)]
    pub description: String,
    /// Fixture copies, relative to the repository root (`from`) and to the
    /// scenario's `source` directory (`to`).
    #[serde(default)]
    pub fixtures: Vec<Fixture>,
    pub steps: Vec<Step>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Fixture {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputKind {
    Json,
    Yaml,
    Text,
    /// Only the exit code is recorded.
    None,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    pub id: String,
    /// CLI arguments. `{source}`, `{home}`, `{work}` and any captured
    /// variable name in braces are substituted before running.
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default = "default_output")]
    pub output: OutputKind,
    /// JSON object keys whose values are masked for this step only, e.g.
    /// version strings that legitimately differ between implementations.
    #[serde(default)]
    pub volatile_keys: Vec<String>,
    /// JSON object keys whose array values are unordered collections (e.g.
    /// graph edges with tied ranks): they are sorted by canonical form.
    #[serde(default)]
    pub unordered_arrays: Vec<String>,
    /// JSON object keys removed before comparison: documented, intentional
    /// implementation-specific fields (e.g. the Python interpreter version).
    #[serde(default)]
    pub drop_keys: Vec<String>,
    /// Captures a variable from stdout for later steps.
    #[serde(default)]
    pub capture: Option<Capture>,
    /// Instead of running the CLI, inventory every SQLite DB under `{home}`.
    #[serde(default)]
    pub sqlite_inventory: bool,
    /// Plan phase that must make the Rust candidate pass this step.
    #[serde(default)]
    pub rust_phase: Option<String>,
    /// Also gate on canonical stderr (default: exit code + stdout only,
    /// since rich/Typer decoration differs from clap's).
    #[serde(default)]
    pub compare_stderr: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Capture {
    pub name: String,
    /// Regex whose first capture group becomes the variable's value.
    pub regex: String,
}

/// Numeric order of a `RUST-NN` phase id.
pub fn phase_number(phase: &str) -> Option<u32> {
    phase.strip_prefix("RUST-")?.parse().ok()
}

fn default_output() -> OutputKind {
    OutputKind::Json
}

#[derive(Debug, thiserror::Error)]
pub enum ManifestError {
    #[error("cannot read manifest {path}: {source}")]
    Io {
        path: String,
        source: std::io::Error,
    },
    #[error("invalid manifest {path}: {source}")]
    Parse {
        path: String,
        source: serde_json::Error,
    },
    #[error("unsupported manifest_version {found} (expected {MANIFEST_VERSION})")]
    Version { found: u32 },
    #[error("invalid manifest: {0}")]
    Invalid(String),
}

impl Manifest {
    pub fn load(path: &Path) -> Result<Self, ManifestError> {
        let display = path.display().to_string();
        let text = std::fs::read_to_string(path).map_err(|source| ManifestError::Io {
            path: display.clone(),
            source,
        })?;
        let manifest: Manifest =
            serde_json::from_str(&text).map_err(|source| ManifestError::Parse {
                path: display,
                source,
            })?;
        manifest.validate()?;
        Ok(manifest)
    }

    pub fn validate(&self) -> Result<(), ManifestError> {
        if self.manifest_version != MANIFEST_VERSION {
            return Err(ManifestError::Version {
                found: self.manifest_version,
            });
        }
        let mut scenario_ids = std::collections::BTreeSet::new();
        for scenario in &self.scenarios {
            if !is_safe_id(&scenario.id) || !scenario_ids.insert(scenario.id.as_str()) {
                return Err(ManifestError::Invalid(format!(
                    "scenario id {:?} is empty, unsafe or duplicated",
                    scenario.id
                )));
            }
            for fixture in &scenario.fixtures {
                if !is_relative_without_parent(&fixture.from)
                    || !is_relative_without_parent(&fixture.to)
                {
                    return Err(ManifestError::Invalid(format!(
                        "fixture paths must be relative without '..': {} -> {}",
                        fixture.from, fixture.to
                    )));
                }
            }
            let mut step_ids = std::collections::BTreeSet::new();
            for step in &scenario.steps {
                if !step_ids.insert(step.id.as_str()) {
                    return Err(ManifestError::Invalid(format!(
                        "duplicate step id {:?} in scenario {}",
                        step.id, scenario.id
                    )));
                }
                if step.sqlite_inventory != step.args.is_empty() {
                    return Err(ManifestError::Invalid(format!(
                        "step {} must have either args or sqlite_inventory, not both/neither",
                        step.id
                    )));
                }
                if let Some(phase) = &step.rust_phase {
                    if phase_number(phase).is_none() {
                        return Err(ManifestError::Invalid(format!(
                            "step {} has invalid rust_phase {phase:?}",
                            step.id
                        )));
                    }
                }
                if let Some(capture) = &step.capture {
                    regex::Regex::new(&capture.regex).map_err(|err| {
                        ManifestError::Invalid(format!("step {} capture regex: {err}", step.id))
                    })?;
                }
            }
        }
        Ok(())
    }
}

fn is_safe_id(id: &str) -> bool {
    !id.is_empty()
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

fn is_relative_without_parent(path: &str) -> bool {
    let p = Path::new(path);
    !path.is_empty()
        && p.is_relative()
        && p.components()
            .all(|c| matches!(c, std::path::Component::Normal(_)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal(steps: &str) -> String {
        format!(
            r#"{{"manifest_version":1,"reference_commit":"x","scenarios":[{{"id":"s","steps":{steps}}}]}}"#
        )
    }

    #[test]
    fn rejects_step_with_args_and_inventory() {
        let m: Manifest = serde_json::from_str(&minimal(
            r#"[{"id":"a","args":["x"],"sqlite_inventory":true}]"#,
        ))
        .unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn rejects_parent_traversal_in_fixtures() {
        let text = r#"{"manifest_version":1,"reference_commit":"x","scenarios":[{"id":"s","fixtures":[{"from":"../etc","to":"a"}],"steps":[]}]}"#;
        let m: Manifest = serde_json::from_str(text).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn accepts_valid_manifest() {
        let m: Manifest =
            serde_json::from_str(&minimal(r#"[{"id":"a","args":["version"]}]"#)).unwrap();
        m.validate().unwrap();
        assert_eq!(m.scenarios[0].steps[0].output, OutputKind::Json);
    }
}
