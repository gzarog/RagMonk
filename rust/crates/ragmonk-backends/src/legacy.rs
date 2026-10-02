//! Python-era (V1) RagMonk index discovery and confirmed deletion.
//!
//! V1 created exactly `{prefix}-files`, `{prefix}-content` and
//! `{prefix}-relationships`. Discovery matches those exact names for the
//! configured prefix and the historical `ragmonk`/`ragpilot` prefixes; it
//! never pattern-deletes, never touches V2 (`{prefix}-v2-*`) or any other
//! index. Deletion requires the fingerprint printed by the preflight, so
//! only the exact set the operator reviewed can be removed: if anything
//! changed in between, deletion is refused.

use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::error::{BackendError, Result};
use crate::transport::{Client, Method};

pub const LEGACY_PREFIXES: &[&str] = &["ragmonk", "ragpilot"];
pub const LEGACY_SUFFIXES: &[&str] = &["files", "content", "relationships"];

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LegacyIndex {
    pub name: String,
    pub docs: Option<u64>,
    pub store_size: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct LegacyReport {
    pub prefixes_checked: Vec<String>,
    pub indexes: Vec<LegacyIndex>,
    /// Pass to [`delete_legacy`] to confirm exactly this set.
    pub fingerprint: String,
}

/// Candidate legacy names for a configured prefix.
pub fn candidate_names(configured_prefix: &str) -> (Vec<String>, Vec<String>) {
    let mut prefixes: Vec<String> = vec![configured_prefix.to_owned()];
    for p in LEGACY_PREFIXES {
        if !prefixes.iter().any(|x| x == p) {
            prefixes.push((*p).to_owned());
        }
    }
    let names = prefixes
        .iter()
        .flat_map(|p| LEGACY_SUFFIXES.iter().map(move |s| format!("{p}-{s}")))
        .collect();
    (prefixes, names)
}

pub fn fingerprint(names: &[String]) -> String {
    let mut sorted = names.to_vec();
    sorted.sort();
    let digest = Sha256::digest(sorted.join("\n").as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// Read-only discovery.
pub fn discover(client: &Client, configured_prefix: &str) -> Result<LegacyReport> {
    let (prefixes, candidates) = candidate_names(configured_prefix);
    let resp = client.send(
        Method::Get,
        "/_cat/indices?format=json&h=index,docs.count,store.size&expand_wildcards=all",
        None,
    )?;
    if resp.status >= 300 {
        return Err(BackendError::Http {
            method: "GET".into(),
            path: "/_cat/indices".into(),
            status: resp.status,
            reason: String::from_utf8_lossy(&resp.body)
                .chars()
                .take(300)
                .collect(),
        });
    }
    let rows = resp.json()?;
    let mut indexes: Vec<LegacyIndex> = rows
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|r| {
            let name = r.get("index")?.as_str()?.to_owned();
            candidates.contains(&name).then(|| LegacyIndex {
                docs: r
                    .get("docs.count")
                    .and_then(Value::as_str)
                    .and_then(|s| s.parse().ok()),
                store_size: r
                    .get("store.size")
                    .and_then(Value::as_str)
                    .map(str::to_owned),
                name,
            })
        })
        .collect();
    indexes.sort_by(|a, b| a.name.cmp(&b.name));
    let names: Vec<String> = indexes.iter().map(|i| i.name.clone()).collect();
    Ok(LegacyReport {
        prefixes_checked: prefixes,
        fingerprint: fingerprint(&names),
        indexes,
    })
}

/// Deletes the legacy indexes, but only if the current set still matches
/// `confirm_fingerprint`. Each index is deleted by its exact name.
pub fn delete_legacy(
    client: &Client,
    configured_prefix: &str,
    confirm_fingerprint: &str,
) -> Result<Vec<String>> {
    let report = discover(client, configured_prefix)?;
    if report.fingerprint != confirm_fingerprint {
        return Err(BackendError::Conflict(format!(
            "legacy index set changed since the preflight (fingerprint {} != {}); re-run the check and review it",
            report.fingerprint, confirm_fingerprint
        )));
    }
    let mut deleted = Vec::new();
    for idx in &report.indexes {
        if idx.name.contains(['*', ',', '?']) || idx.name.starts_with('_') {
            return Err(BackendError::Invalid(format!(
                "refusing suspicious index name {:?}",
                idx.name
            )));
        }
        let path = format!("/{}", idx.name);
        let resp = client.send(Method::Delete, &path, None)?;
        if resp.status >= 300 && resp.status != 404 {
            return Err(BackendError::Http {
                method: "DELETE".into(),
                path,
                status: resp.status,
                reason: String::from_utf8_lossy(&resp.body)
                    .chars()
                    .take(300)
                    .collect(),
            });
        }
        deleted.push(idx.name.clone());
    }
    Ok(deleted)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidates_are_exact_and_exclude_v2() {
        let (prefixes, names) = candidate_names("team");
        assert_eq!(prefixes, vec!["team", "ragmonk", "ragpilot"]);
        assert!(names.contains(&"team-content".to_string()));
        assert!(names.contains(&"ragpilot-relationships".to_string()));
        assert!(!names.iter().any(|n| n.contains("-v2")));
        assert_eq!(names.len(), 9);
        let (_, dedup) = candidate_names("ragmonk");
        assert_eq!(dedup.len(), 6);
    }

    #[test]
    fn fingerprint_is_order_independent() {
        let a = vec!["x".to_string(), "y".to_string()];
        let b = vec!["y".to_string(), "x".to_string()];
        assert_eq!(fingerprint(&a), fingerprint(&b));
        assert_ne!(fingerprint(&a), fingerprint(&["x".to_string()]));
    }
}
