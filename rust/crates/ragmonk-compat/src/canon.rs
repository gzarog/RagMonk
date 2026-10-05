//! Canonicalization of captured CLI output.
//!
//! Rules (plan `testing_strategy.canonicalization_rules`):
//! * nondeterministic timestamps/durations/PIDs are masked;
//! * object keys are sorted (`serde_json::Map` is ordered);
//! * environment-specific paths become `<WORK>` / `<HOME>`;
//! * random opaque IDs (`uuid4().hex`, used by the Python local backend
//!   for files/entities/documents/jobs) become `<ID>` -- only their shape
//!   is a compatibility contract, not their value;
//! * floats are kept verbatim and compared with tolerance in `compare`.
//!
//! In [`IdMode::Portable`] path-derived deterministic IDs (source and
//! project IDs) are additionally masked through [`Masks::literals`] so a
//! baseline captured on one machine is comparable on another. In
//! [`IdMode::Strict`] they are kept, for same-machine Python-vs-Rust runs.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Map, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IdMode {
    Portable,
    Strict,
}

/// Object keys whose values are always nondeterministic.
const VOLATILE_KEYS: &[&str] = &["pid", "hostname", "python", "running_for_seconds"];
/// Key suffixes whose values are always nondeterministic.
const VOLATILE_SUFFIXES: &[&str] = &[
    "_at",
    "_age_seconds",
    "_ms",
    "duration_seconds",
    "elapsed_seconds",
];

/// Literal replacements applied to every string, longest first.
#[derive(Debug, Default, Clone)]
pub struct Masks {
    literals: Vec<(String, String)>,
}

impl Masks {
    pub fn add(&mut self, needle: impl Into<String>, replacement: impl Into<String>) {
        let needle = needle.into();
        if needle.is_empty() || self.literals.iter().any(|(n, _)| *n == needle) {
            return;
        }
        self.literals.push((needle, replacement.into()));
        self.literals.sort_by(|a, b| b.0.len().cmp(&a.0.len()));
    }

    pub fn apply_str(&self, input: &str) -> String {
        let mut out = input.to_owned();
        for (needle, replacement) in &self.literals {
            if out.contains(needle.as_str()) {
                out = out.replace(needle.as_str(), replacement);
            }
            // Windows paths may appear with either separator.
            let alt = needle.replace('\\', "/");
            if alt != *needle && out.contains(alt.as_str()) {
                out = out.replace(alt.as_str(), replacement);
            }
        }
        let out = timestamp_re().replace_all(&out, "<TS>");
        let out = uuid_hex_re().replace_all(&out, "<ID>");
        out.into_owned()
    }
}

fn timestamp_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
            .expect("static regex")
    })
}

fn uuid_hex_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"\b[0-9a-f]{32}\b").expect("static regex"))
}

fn is_volatile_key(key: &str, extra: &[String]) -> bool {
    VOLATILE_KEYS.contains(&key)
        || VOLATILE_SUFFIXES.iter().any(|s| key.ends_with(s))
        || extra.iter().any(|k| k == key)
}

/// Canonicalizes a JSON value in place semantics (returns a new value).
pub fn canonical_json(value: &Value, masks: &Masks, volatile_keys: &[String]) -> Value {
    canonical_json_with(value, masks, volatile_keys, &[])
}

/// Like [`canonical_json`], additionally sorting arrays stored under any key
/// in `unordered` by their canonical JSON text.
pub fn canonical_json_with(
    value: &Value,
    masks: &Masks,
    volatile_keys: &[String],
    unordered: &[String],
) -> Value {
    canonical_json_full(value, masks, volatile_keys, unordered, &[])
}

/// [`canonical_json_with`] that also drops object keys listed in `drop`.
pub fn canonical_json_full(
    value: &Value,
    masks: &Masks,
    volatile_keys: &[String],
    unordered: &[String],
    drop: &[String],
) -> Value {
    match value {
        Value::Object(map) => {
            let mut out = Map::new();
            for (key, inner) in map {
                if drop.iter().any(|d| d == key) {
                    continue;
                }
                let canon = if is_volatile_key(key, volatile_keys) && !inner.is_null() {
                    Value::String("<VOLATILE>".into())
                } else {
                    let mut canon =
                        canonical_json_full(inner, masks, volatile_keys, unordered, drop);
                    if let Value::Array(items) = &mut canon {
                        if unordered.iter().any(|k| k == key) {
                            items.sort_by_cached_key(sorted_text);
                        }
                    }
                    canon
                };
                out.insert(masks.apply_str(key), canon);
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|v| canonical_json_full(v, masks, volatile_keys, unordered, drop))
                .collect(),
        ),
        Value::String(s) => Value::String(masks.apply_str(s)),
        other => other.clone(),
    }
}

/// JSON text with object keys sorted at every level, so the order of
/// unordered arrays does not depend on either implementation's key order.
fn sorted_text(v: &Value) -> String {
    match v {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let parts: Vec<String> = keys
                .into_iter()
                .map(|k| format!("{}:{}", Value::String(k.clone()), sorted_text(&map[k])))
                .collect();
            format!("{{{}}}", parts.join(","))
        }
        Value::Array(items) => {
            let parts: Vec<String> = items.iter().map(sorted_text).collect();
            format!("[{}]", parts.join(","))
        }
        other => other.to_string(),
    }
}

/// Canonicalizes free text: masks, trims trailing whitespace, drops
/// trailing blank lines and normalizes line endings.
pub fn canonical_text(text: &str, masks: &Masks) -> Vec<String> {
    let masked = masks.apply_str(&text.replace("\r\n", "\n"));
    let mut lines: Vec<String> = masked.lines().map(|l| l.trim_end().to_owned()).collect();
    while lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn masks_volatile_keys_timestamps_ids_and_paths() {
        let mut masks = Masks::default();
        masks.add("/tmp/work", "<WORK>");
        let raw = json!({
            "pid": 42,
            "started_at": "2026-10-02T16:30:06.463119+00:00",
            "error": null,
            "completed_at": null,
            "id": "b2f7a965b1734d1fbffe272f5a55fe59",
            "path": "/tmp/work/src/a.py",
            "note": "ran at 2026-10-02 16:30:06",
            "score": 0.5,
        });
        let canon = canonical_json(&raw, &masks, &[]);
        assert_eq!(
            canon,
            json!({
                "completed_at": null,
                "error": null,
                "id": "<ID>",
                "note": "ran at <TS>",
                "path": "<WORK>/src/a.py",
                "pid": "<VOLATILE>",
                "score": 0.5,
                "started_at": "<VOLATILE>",
            })
        );
    }

    #[test]
    fn unordered_arrays_are_sorted_only_under_named_keys() {
        let masks = Masks::default();
        let a = json!({"edges": [{"e": "this"}, {"e": "self"}], "ranked": [2, 1]});
        let b = json!({"edges": [{"e": "self"}, {"e": "this"}], "ranked": [2, 1]});
        let unordered = vec!["edges".to_string()];
        let ca = canonical_json_with(&a, &masks, &[], &unordered);
        assert_eq!(ca, canonical_json_with(&b, &masks, &[], &unordered));
        assert_eq!(ca["ranked"], json!([2, 1]));
        // Key order inside the elements does not affect their order.
        let c: Value =
            serde_json::from_str(r#"{"edges": [{"b": 1, "a": "y"}, {"a": "x", "b": 2}]}"#).unwrap();
        let d: Value =
            serde_json::from_str(r#"{"edges": [{"a": "y", "b": 1}, {"b": 2, "a": "x"}]}"#).unwrap();
        assert_eq!(
            canonical_json_with(&c, &masks, &[], &unordered)["edges"][0]["a"],
            canonical_json_with(&d, &masks, &[], &unordered)["edges"][0]["a"]
        );
    }

    #[test]
    fn drop_keys_are_removed_everywhere() {
        let masks = Masks::default();
        let raw = json!({"data": {"python": "3.12", "version": "1", "nested": [{"python": 1}]}});
        let canon = canonical_json_full(&raw, &masks, &[], &[], &["python".to_string()]);
        assert_eq!(canon, json!({"data": {"nested": [{}], "version": "1"}}));
    }

    #[test]
    fn longest_literal_wins() {
        let mut masks = Masks::default();
        masks.add("/w", "<WORK>");
        masks.add("/w/home", "<HOME>");
        assert_eq!(masks.apply_str("/w/home/x /w/y"), "<HOME>/x <WORK>/y");
    }

    #[test]
    fn text_is_trimmed_and_masked() {
        let masks = Masks::default();
        let lines = canonical_text("a  \r\nb\n\n", &masks);
        assert_eq!(lines, vec!["a", "b"]);
    }

    #[test]
    fn canonicalization_is_idempotent() {
        let masks = Masks::default();
        let raw = json!({"b": [1, {"z_at": "x", "a": "2026-01-01T00:00:00Z"}]});
        let once = canonical_json(&raw, &masks, &[]);
        assert_eq!(canonical_json(&once, &masks, &[]), once);
    }
}
