//! The Configuration page (`service/config_service.py`): every config
//! section as typed fields with env overrides read-only and sensitive
//! values never shown, and form updates validated exactly like the CLI.

use ragmonk_config::loader::deep_merge;
use ragmonk_config::model::RagMonkConfig;
use ragmonk_config::pyvalue::{dict_set, PyValue};
use ragmonk_core::paths::Home;
use serde_json::{json, Value};

use crate::load;

const ENV_PREFIX: &str = "RAGMONK_";
const SENSITIVE: &[&str] = &["key", "token", "secret", "password", "credential"];

fn is_sensitive(path: &str) -> bool {
    let lower = path.to_lowercase();
    SENSITIVE.iter().any(|t| lower.contains(t))
}

fn env_var_for(path: &str) -> String {
    format!("{ENV_PREFIX}{}", path.replace('.', "__").to_uppercase())
}

fn kind_of(v: &PyValue) -> Option<&'static str> {
    match v {
        PyValue::Bool(_) => Some("bool"),
        PyValue::Int(_) => Some("int"),
        PyValue::Float(_) => Some("float"),
        PyValue::Str(_) => Some("str"),
        PyValue::List(items) if items.iter().all(|i| matches!(i, PyValue::Str(_))) => Some("list"),
        _ => None,
    }
}

fn to_json(v: &PyValue) -> Value {
    match v {
        PyValue::None => Value::Null,
        PyValue::Bool(b) => json!(b),
        PyValue::Int(i) => i64::try_from(*i).map_or_else(|_| json!(i.to_string()), |i| json!(i)),
        PyValue::Float(f) => json!(f),
        PyValue::Str(s) | PyValue::Timestamp(s) => json!(s),
        PyValue::Bytes(_) => Value::Null,
        PyValue::List(items) => Value::Array(items.iter().map(to_json).collect()),
        PyValue::Dict(d) => Value::Object(
            d.iter()
                .filter_map(|(k, v)| match k {
                    PyValue::Str(k) => Some((k.clone(), to_json(v))),
                    _ => None,
                })
                .collect(),
        ),
    }
}

fn truthy(v: &PyValue) -> bool {
    match v {
        PyValue::None => false,
        PyValue::Bool(b) => *b,
        PyValue::Int(i) => *i != 0,
        PyValue::Float(f) => *f != 0.0,
        PyValue::Str(s) | PyValue::Timestamp(s) => !s.is_empty(),
        PyValue::Bytes(b) => !b.is_empty(),
        PyValue::List(l) => !l.is_empty(),
        PyValue::Dict(d) => !d.is_empty(),
    }
}

/// Python `str()` of a scalar config value.
fn py_str(v: &PyValue) -> String {
    match v {
        PyValue::Bool(true) => "True".into(),
        PyValue::Bool(false) => "False".into(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => ragmonk_ai::pyfmt::repr(&json!(f)),
        PyValue::Str(s) => s.clone(),
        other => format!("{other:?}"),
    }
}

fn describe(dict: &[(PyValue, PyValue)], prefix: &str, env: &dyn Fn(&str) -> bool) -> Vec<Value> {
    let mut fields = Vec::new();
    for (k, value) in dict {
        let PyValue::Str(name) = k else { continue };
        let path = if prefix.is_empty() {
            name.clone()
        } else {
            format!("{prefix}.{name}")
        };
        if let PyValue::Dict(inner) = value {
            fields.push(json!({
                "name": name,
                "path": path,
                "kind": "group",
                "fields": describe(inner, &path, env),
            }));
            continue;
        }
        let Some(kind) = kind_of(value) else { continue };
        let env_var = env_var_for(&path);
        let overridden = env(&env_var);
        let sensitive = is_sensitive(&path);
        let display = if sensitive {
            if truthy(value) {
                "Configured"
            } else {
                "Not configured"
            }
            .to_owned()
        } else if let PyValue::List(items) = value {
            items.iter().map(py_str).collect::<Vec<_>>().join(", ")
        } else {
            py_str(value)
        };
        fields.push(json!({
            "name": name,
            "path": path,
            "kind": kind,
            "value": if sensitive { Value::Null } else { to_json(value) },
            "display": display,
            "sensitive": sensitive,
            "env_overridden": overridden,
            "env_var": if overridden { json!(env_var) } else { Value::Null },
            "editable": !overridden && !sensitive,
        }));
    }
    fields
}

fn env_present(name: &str) -> bool {
    std::env::var_os(name).is_some()
}

/// `config_service.describe_config`.
pub fn describe_config(home: &Home) -> Result<Vec<Value>, ragmonk_core::errors::RagMonkError> {
    let cfg = load(home)?;
    let dumped = cfg.to_pyvalue();
    let mut sections = Vec::new();
    for (k, v) in dumped.as_dict().into_iter().flatten() {
        if let (PyValue::Str(name), PyValue::Dict(inner)) = (k, v) {
            sections.push(json!({"name": name, "fields": describe(inner, name, &env_present)}));
        }
    }
    Ok(sections)
}

fn kinds(sections: &[Value]) -> Vec<(String, String)> {
    fn walk(fields: &Value, out: &mut Vec<(String, String)>) {
        for f in fields.as_array().into_iter().flatten() {
            if f["kind"] == "group" {
                walk(&f["fields"], out);
            } else if let (Some(p), Some(k)) = (f["path"].as_str(), f["kind"].as_str()) {
                out.push((p.to_owned(), k.to_owned()));
            }
        }
    }
    let mut out = Vec::new();
    for s in sections {
        walk(&s["fields"], &mut out);
    }
    out
}

/// Python's `int(raw)` / `float(raw)` coercions and their messages.
fn coerce(kind: &str, raw: &str) -> Result<PyValue, String> {
    let raw = raw.trim();
    Ok(match kind {
        "bool" => PyValue::Bool(matches!(
            raw.to_lowercase().as_str(),
            "true" | "1" | "on" | "yes"
        )),
        "int" => PyValue::Int(
            ragmonk_config::coerce::parse_python_int(raw)
                .ok_or_else(|| format!("invalid literal for int() with base 10: '{raw}'"))?,
        ),
        "float" => PyValue::Float(
            ragmonk_config::coerce::parse_python_float(raw)
                .ok_or_else(|| format!("could not convert string to float: '{raw}'"))?,
        ),
        "list" => PyValue::List(
            raw.replace('\n', ",")
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(PyValue::str)
                .collect(),
        ),
        _ => PyValue::str(raw),
    })
}

fn set_path(target: &mut PyValue, path: &str, value: PyValue) {
    let mut parts: Vec<&str> = path.split('.').collect();
    let last = parts.pop().unwrap_or_default();
    let mut cursor = target;
    for part in parts {
        let PyValue::Dict(d) = cursor else { return };
        if !d
            .iter()
            .any(|(k, _)| matches!(k, PyValue::Str(s) if s == part))
        {
            d.push((PyValue::str(part), PyValue::Dict(Vec::new())));
        }
        let slot = d
            .iter_mut()
            .find(|(k, _)| matches!(k, PyValue::Str(s) if s == part))
            .map(|(_, v)| v);
        match slot {
            Some(v @ PyValue::Dict(_)) => cursor = v,
            Some(v) => {
                *v = PyValue::Dict(Vec::new());
                cursor = v;
            }
            None => return,
        }
    }
    if let PyValue::Dict(d) = cursor {
        dict_set(d, PyValue::str(last), value);
    }
}

/// `config_service.apply_updates`: validates, then writes the user
/// config. Sensitive and env-overridden fields are ignored. The error
/// string is what the page shows.
pub fn apply_updates(home: &Home, form: &[(String, String)]) -> Result<(), String> {
    let mut user = match std::fs::read_to_string(home.user_config()) {
        Ok(text) => ragmonk_config::yaml_load::safe_load(&text)
            .map_err(|e| e.0)?
            .as_dict()
            .map(|d| PyValue::Dict(d.clone()))
            .unwrap_or(PyValue::Dict(Vec::new())),
        Err(_) => PyValue::Dict(Vec::new()),
    };
    let sections = describe_config(home).map_err(|e| e.message().to_owned())?;
    let kinds = kinds(&sections);
    for (key, raw) in form {
        let path = key.replace("__", ".");
        let Some((_, kind)) = kinds.iter().find(|(p, _)| *p == path) else {
            continue;
        };
        if is_sensitive(&path) || env_present(&env_var_for(&path)) {
            continue;
        }
        set_path(&mut user, &path, coerce(kind, raw)?);
    }
    let merged = deep_merge(&RagMonkConfig::default().to_pyvalue(), &user);
    let validated = RagMonkConfig::validate(&merged).map_err(|e| e.render())?;
    ragmonk_config::loader::write_user_config(&validated, home).map_err(|e| e.to_string())?;
    Ok(())
}
