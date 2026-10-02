//! Layered configuration loading (`ragmonk.core.config.load_config`).
//!
//! Precedence, highest wins: CLI overrides > environment variables
//! (`RAGMONK_` prefix, `__` nesting) > project config (`./.ragmonk.yaml`)
//! > user config (`<home>/config.yaml`) > built-in defaults.

use std::path::{Path, PathBuf};

use ragmonk_core::errors::RagMonkError;
use ragmonk_core::paths::{project_config_path, Home};

use crate::coerce::{parse_python_float, parse_python_int};
use crate::model::{RagMonkConfig, KNOWN_SECTIONS};
use crate::pyvalue::{dict_set, PyValue};
use crate::yaml_emit::safe_dump;
use crate::yaml_load::safe_load;

const ENV_PREFIX: &str = "RAGMONK_";

/// Inputs to [`load_config`]; `None` fields mean "use the process value".
#[derive(Debug, Clone, Default)]
pub struct LoadOptions {
    pub home: Option<PathBuf>,
    pub cwd: Option<PathBuf>,
    /// Ordered `(name, value)` pairs; `None` reads the process environment.
    pub environ: Option<Vec<(String, String)>>,
    pub cli_overrides: Option<PyValue>,
}

fn load_yaml_file(path: &Path) -> Result<PyValue, RagMonkError> {
    if !path.is_file() {
        return Ok(PyValue::Dict(Vec::new()));
    }
    let read_err = |e: &dyn std::fmt::Display| {
        RagMonkError::config(format!(
            "failed to read config file {}: {e}",
            path.display()
        ))
    };
    let raw = std::fs::read_to_string(path).map_err(|e| read_err(&e))?;
    match safe_load(&raw).map_err(|e| read_err(&e))? {
        PyValue::None => Ok(PyValue::Dict(Vec::new())),
        dict @ PyValue::Dict(_) => Ok(dict),
        _ => Err(RagMonkError::config(format!(
            "config file {} must contain a mapping at the top level",
            path.display()
        ))),
    }
}

/// `_coerce_scalar`: env values become bool, int, float or stay strings.
pub fn coerce_env_scalar(raw: &str) -> PyValue {
    let lowered = raw.trim().to_lowercase();
    if lowered == "true" || lowered == "false" {
        return PyValue::Bool(lowered == "true");
    }
    if let Some(i) = parse_python_int(raw) {
        return PyValue::Int(i);
    }
    if let Some(f) = parse_python_float(raw) {
        return PyValue::Float(f);
    }
    PyValue::Str(raw.to_owned())
}

/// `_env_overrides`.
pub fn env_overrides(environ: &[(String, String)]) -> PyValue {
    let mut overrides = PyValue::Dict(Vec::new());
    for (key, value) in environ {
        let Some(remainder) = key.strip_prefix(ENV_PREFIX) else {
            continue;
        };
        if !remainder.contains("__") {
            continue;
        }
        let segments: Vec<String> = remainder
            .split("__")
            .filter(|s| !s.is_empty())
            .map(str::to_lowercase)
            .collect();
        let Some(first) = segments.first() else {
            continue;
        };
        if !KNOWN_SECTIONS.contains(&first.as_str()) {
            continue;
        }
        set_nested(&mut overrides, &segments, coerce_env_scalar(value));
    }
    overrides
}

fn set_nested(root: &mut PyValue, segments: &[String], value: PyValue) {
    let PyValue::Dict(dict) = root else {
        // Python would raise TypeError indexing a non-dict; skip instead.
        return;
    };
    let key = PyValue::Str(segments[0].clone());
    if segments.len() == 1 {
        dict_set(dict, key, value);
        return;
    }
    if !dict.iter().any(|(k, _)| k.key_eq(&key)) {
        dict.push((key.clone(), PyValue::Dict(Vec::new())));
    }
    if let Some((_, child)) = dict.iter_mut().find(|(k, _)| k.key_eq(&key)) {
        set_nested(child, &segments[1..], value);
    }
}

/// `_deep_merge`.
pub fn deep_merge(base: &PyValue, over: &PyValue) -> PyValue {
    let (PyValue::Dict(b), PyValue::Dict(o)) = (base, over) else {
        return over.clone();
    };
    let mut result = b.clone();
    for (key, value) in o {
        let existing = result
            .iter()
            .find(|(k, _)| k.key_eq(key))
            .map(|(_, v)| v.clone());
        let merged = match (value, existing) {
            (PyValue::Dict(_), Some(prev @ PyValue::Dict(_))) => deep_merge(&prev, value),
            _ => value.clone(),
        };
        dict_set(&mut result, key.clone(), merged);
    }
    PyValue::Dict(result)
}

/// The merged, not-yet-validated layers.
pub fn merged_layers(opts: &LoadOptions) -> Result<PyValue, RagMonkError> {
    let home = opts
        .home
        .clone()
        .map(Home::new)
        .unwrap_or_else(Home::discover);
    let cwd = match &opts.cwd {
        Some(c) => c.clone(),
        None => std::env::current_dir().map_err(|e| RagMonkError::config(e.to_string()))?,
    };
    let environ = opts.environ.clone().unwrap_or_else(|| {
        std::env::vars_os()
            .map(|(k, v)| {
                (
                    k.to_string_lossy().into_owned(),
                    v.to_string_lossy().into_owned(),
                )
            })
            .collect()
    });
    let defaults = RagMonkConfig::default().to_pyvalue();
    let user = load_yaml_file(&home.user_config())?;
    let project = load_yaml_file(&project_config_path(&cwd))?;
    let env = env_overrides(&environ);
    let mut merged = deep_merge(&defaults, &user);
    merged = deep_merge(&merged, &project);
    merged = deep_merge(&merged, &env);
    if let Some(cli) = &opts.cli_overrides {
        merged = deep_merge(&merged, cli);
    }
    Ok(merged)
}

/// `load_config`. Validation errors become a `ConfigError` (exit code 3)
/// whose message never echoes input values and has URL user-info scrubbed.
pub fn load_config(opts: &LoadOptions) -> Result<RagMonkConfig, RagMonkError> {
    let merged = merged_layers(opts)?;
    RagMonkConfig::validate(&merged).map_err(|errors| {
        let message = scrub_url_userinfo(&errors.render());
        RagMonkError::config(format!("invalid configuration: {message}"))
    })
}

fn scrub_url_userinfo(text: &str) -> String {
    use std::sync::OnceLock;
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@]+@").expect("static regex")
    })
    .replace_all(text, "$1")
    .into_owned()
}

/// `yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)`.
pub fn dump_yaml(config: &RagMonkConfig) -> String {
    safe_dump(&config.to_pyvalue())
}

/// `write_user_config`.
pub fn write_user_config(config: &RagMonkConfig, home: &Home) -> std::io::Result<PathBuf> {
    let path = home.user_config();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&path, dump_yaml(config))?;
    Ok(path)
}

/// `config get`: the dumped value at a dotted key.
pub fn get_path(config: &RagMonkConfig, dotted: &str) -> Result<PyValue, RagMonkError> {
    let unknown = || RagMonkError::usage(format!("unknown config key: {dotted}"));
    let mut cursor = config.to_pyvalue();
    for segment in dotted.split('.') {
        cursor = cursor.get(segment).cloned().ok_or_else(unknown)?;
    }
    Ok(cursor)
}

/// `config set` coercion + validation. Returns the validated config and
/// the stored value. Errors keep the reference's exit codes: unknown key
/// = 2, unparsable number or failed validation = 1.
pub fn set_path(
    config: &RagMonkConfig,
    dotted: &str,
    raw: &str,
) -> Result<(RagMonkConfig, PyValue), RagMonkError> {
    let unknown = || RagMonkError::usage(format!("unknown config key: {dotted}"));
    let mut data = config.to_pyvalue();
    let segments: Vec<&str> = dotted.split('.').collect();
    let (last, parents) = segments.split_last().ok_or_else(unknown)?;
    let mut cursor = &mut data;
    for segment in parents {
        let PyValue::Dict(entries) = cursor else {
            return Err(unknown());
        };
        cursor = entries
            .iter_mut()
            .find(|(k, v)| {
                matches!(k, PyValue::Str(s) if s == segment) && matches!(v, PyValue::Dict(_))
            })
            .map(|(_, v)| v)
            .ok_or_else(unknown)?;
    }
    let PyValue::Dict(entries) = cursor else {
        return Err(unknown());
    };
    let slot = entries
        .iter_mut()
        .find(|(k, _)| matches!(k, PyValue::Str(s) if s == last))
        .map(|(_, v)| v)
        .ok_or_else(unknown)?;
    let generic = |m: String| RagMonkError::new(ragmonk_core::ErrorKind::Generic, m);
    let new_value = match slot {
        PyValue::Bool(_) => PyValue::Bool(matches!(
            raw.trim().to_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        )),
        PyValue::Int(_) => PyValue::Int(parse_python_int(raw).ok_or_else(|| {
            generic(format!(
                "invalid literal for int() with base 10: {}",
                crate::pyvalue::py_repr_str(raw)
            ))
        })?),
        PyValue::Float(_) => PyValue::Float(parse_python_float(raw).ok_or_else(|| {
            generic(format!(
                "could not convert string to float: {}",
                crate::pyvalue::py_repr_str(raw)
            ))
        })?),
        PyValue::List(_) => PyValue::List(
            raw.split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(PyValue::str)
                .collect(),
        ),
        _ => PyValue::str(raw),
    };
    *slot = new_value.clone();
    let updated = RagMonkConfig::validate(&data).map_err(|errors| {
        generic(format!(
            "invalid configuration: {}",
            scrub_url_userinfo(&errors.render())
        ))
    })?;
    Ok((updated, new_value))
}

/// Python `str(value)` as `rich` prints a scalar `config get` result.
pub fn python_str(value: &PyValue) -> Option<String> {
    Some(match value {
        PyValue::None => "None".into(),
        PyValue::Bool(b) => if *b { "True" } else { "False" }.into(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) if f.is_nan() => "nan".into(),
        PyValue::Float(f) if f.is_infinite() => if *f > 0.0 { "inf" } else { "-inf" }.into(),
        PyValue::Float(f) => ragmonk_telemetry::logging::python_float_repr(*f),
        PyValue::Str(s) => s.clone(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn env_scalar_coercion() {
        assert_eq!(coerce_env_scalar(" TrUe "), PyValue::Bool(true));
        assert_eq!(coerce_env_scalar("1_0"), PyValue::Int(10));
        assert_eq!(coerce_env_scalar("1e3"), PyValue::Float(1000.0));
        assert_eq!(coerce_env_scalar("0x4"), PyValue::str("0x4"));
        assert_eq!(coerce_env_scalar(""), PyValue::str(""));
    }

    #[test]
    fn set_and_get() {
        let cfg = RagMonkConfig::default();
        let (cfg, v) = set_path(&cfg, "runtime.max_workers", "3").unwrap();
        assert_eq!(v, PyValue::Int(3));
        assert_eq!(cfg.runtime.max_workers, 3);
        let (cfg, _) = set_path(&cfg, "search.output.fallback", "json, files,").unwrap();
        assert_eq!(cfg.search.output.fallback, vec!["json", "files"]);
        let (cfg, _) = set_path(&cfg, "ai.base_url", "http://x").unwrap();
        assert_eq!(cfg.ai.base_url.as_deref(), Some("http://x"));
        assert_eq!(set_path(&cfg, "no.such", "1").unwrap_err().exit_code(), 2);
        assert_eq!(
            set_path(&cfg, "runtime.max_workers", "x")
                .unwrap_err()
                .exit_code(),
            1
        );
        assert_eq!(
            set_path(&cfg, "storage.mode", "bogus")
                .unwrap_err()
                .exit_code(),
            1
        );
        assert_eq!(
            get_path(&cfg, "runtime.max_workers").unwrap(),
            PyValue::Int(3)
        );
        assert_eq!(get_path(&cfg, "runtime.nope").unwrap_err().exit_code(), 2);
        assert_eq!(
            python_str(&get_path(&cfg, "indexing.watch").unwrap()).unwrap(),
            "True"
        );
    }
}
