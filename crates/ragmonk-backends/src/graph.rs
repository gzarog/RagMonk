//! Server-side relationship graph generations.
//!
//! The base index and the relationship graph are published independently.
//! A graph generation is a set of `edge`/`link` records in the
//! relationships index whose `build_id` is the generation id (never a base
//! build id), plus a record in the source-state document under
//! `versions.graph` (the opaque `versions` object, so the strict mappings
//! are unchanged):
//!
//! ```json
//! { "state": "ready", "generation": "<id>", "base_build_id": "<base>",
//!   "digest": "...", "published_at": "...", "retired": [...] }
//! ```
//!
//! [`ServerBackend::publish_graph`] writes a generation invisibly (files
//! whose graph records did not change since the previous generation are
//! copied forward server-side, in place: the records gain the new
//! generation id; only changed files' records are bulk-written), then
//! promotes it with one compare-and-swap of the state document that is
//! fenced by the writer lease **and** by the expected active base build: a
//! graph derived from a superseded base is never promoted. Readers
//! ([`ServerBackend::visible_graph`]) only use a generation that is `ready`
//! and whose base is the source's active build. Base publication never
//! writes graph records, and graph publication never touches the base build
//! pointer or base records.

use std::collections::{BTreeMap, HashMap};
use std::time::Instant;

use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use ragmonk_storage::knowledge::ProjectStore;

use crate::backend::{check_lease, EdgeDoc, Lease, LinkDoc, ServerBackend, SourceState};
use crate::error::{BackendError, Result};
use crate::schema::IndexKind;

/// Key of the graph record inside the state document's `versions`.
pub const GRAPH_KEY: &str = "graph";

/// The graph record of one source (see the module docs).
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct ServerGraph {
    pub state: String,
    pub generation: Option<String>,
    pub base_build_id: Option<String>,
    pub digest: Option<String>,
    /// Semantic digest of the inputs the promoted generation was derived
    /// from (see `ragmonk_storage::graph::graph_input_digest`).
    pub input_digest: Option<String>,
    pub pending: Option<String>,
    pub published_at: Option<String>,
    pub last_error: Option<String>,
    pub stale_reason: Option<String>,
    /// `file_id -> digest` of the promoted generation's records, used to
    /// copy unchanged files' records forward.
    #[serde(skip)]
    pub file_digests: BTreeMap<String, String>,
    /// Retired generations still inside the reader grace window.
    #[serde(skip)]
    pub retired: Vec<String>,
}

impl ServerGraph {
    pub fn from_state(state: &SourceState) -> Self {
        let g = state
            .doc
            .get("versions")
            .and_then(|v| v.get(GRAPH_KEY))
            .cloned()
            .unwrap_or(Value::Null);
        let s = |k: &str| g.get(k).and_then(Value::as_str).map(str::to_owned);
        Self {
            state: s("state").unwrap_or_else(|| "pending".into()),
            generation: s("generation"),
            base_build_id: s("base_build_id"),
            digest: s("digest"),
            input_digest: s("input_digest"),
            pending: s("pending"),
            published_at: s("published_at"),
            last_error: s("last_error"),
            stale_reason: s("stale_reason"),
            file_digests: g
                .get("file_digests")
                .and_then(Value::as_object)
                .map(|m| {
                    m.iter()
                        .filter_map(|(k, v)| Some((k.clone(), v.as_str()?.to_owned())))
                        .collect()
                })
                .unwrap_or_default(),
            retired: g
                .get("retired")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|r| r["generation"].as_str().map(str::to_owned))
                .collect(),
        }
    }

    /// The effective state against the source's active base build.
    pub fn effective_state(&self, active: Option<&str>) -> &str {
        if self.state == "ready" && self.base_build_id.as_deref() != active {
            "stale"
        } else {
            &self.state
        }
    }

    /// The generation readers may use for `active`, if any.
    pub fn visible_generation(&self, active: Option<&str>) -> Option<&str> {
        (self.state == "ready" && active.is_some() && self.base_build_id.as_deref() == active)
            .then_some(self.generation.as_deref())
            .flatten()
    }
}

/// What to publish.
pub struct GraphPublishInput<'a> {
    pub source_id: &'a str,
    pub store: &'a ProjectStore,
    /// The staged build whose graph rows are published.
    pub local_build: &'a str,
    /// The server's active base build the graph was derived from.
    pub base_build_id: &'a str,
    pub lease: Option<&'a Lease>,
    /// The new graph generation id.
    pub generation: &'a str,
    /// Semantic digest of the staged graph's inputs, recorded with the
    /// generation so an unchanged source is recognized without staging.
    pub input_digest: Option<&'a str>,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct GraphPublishReport {
    pub generation: Option<String>,
    /// `false` when the server already had exactly this graph.
    pub published: bool,
    pub edges_written: usize,
    pub links_written: usize,
    /// Files whose graph records were copied forward server-side (the
    /// previous generation had exactly these records).
    pub files_copied: usize,
    /// Files whose graph records were written.
    pub files_written: usize,
    /// Records copied forward (gained the new generation id in place).
    pub records_copied: u64,
    pub seconds: f64,
}

fn storage(e: ragmonk_storage::StorageError) -> BackendError {
    BackendError::Invalid(format!("staging store: {e}"))
}

/// One file's graph records (edges recorded by the file, links into its
/// documents) and their digest.
#[derive(Default)]
struct FileGraph {
    edges: Vec<EdgeDoc>,
    links: Vec<LinkDoc>,
    digest: String,
}

/// The staged graph grouped by file, plus the overall digest (which also
/// covers the base build, so a new base always gets its own generation).
struct StagedGraph {
    files: BTreeMap<String, FileGraph>,
    digest: String,
}

fn stage(store: &ProjectStore, build: &str, base: &str) -> Result<StagedGraph> {
    let files = store.files(build).map_err(storage)?;
    let paths: HashMap<&str, &str> = files
        .iter()
        .map(|f| (f.id.as_str(), f.rel_path.as_str()))
        .collect();
    let docs = store.documents(build).map_err(storage)?;
    let doc_file: HashMap<&str, &str> = docs
        .iter()
        .map(|d| (d.id.as_str(), d.file_id.as_str()))
        .collect();
    let mut out: BTreeMap<String, FileGraph> = BTreeMap::new();
    for f in &files {
        for r in store.file_relationships(build, &f.id).map_err(storage)? {
            out.entry(f.id.clone()).or_default().edges.push(EdgeDoc {
                relationship_id: r.id,
                file_id: f.id.clone(),
                rel_path: f.rel_path.clone(),
                relationship_type: r.relationship_type,
                source_entity_id: r.source_entity_id,
                target_entity_id: r.target_entity_id,
                target_symbol: r.target_symbol,
                resolver: r.resolver,
                confidence: r.confidence,
                source_location: r.source_location,
                evidence: r.evidence,
                reference_text: r.reference_text,
            });
        }
    }
    for l in store.links(build).map_err(storage)? {
        let file_id = doc_file
            .get(l.document_id.as_str())
            .copied()
            .unwrap_or_default();
        out.entry(file_id.to_owned())
            .or_default()
            .links
            .push(LinkDoc {
                relationship_id: l.id,
                relationship_type: l.link_type,
                entity_id: l.entity_id,
                document_id: l.document_id,
                chunk_id: l.chunk_id,
                file_id: file_id.to_owned(),
                rel_path: paths.get(file_id).copied().unwrap_or_default().to_owned(),
                resolver: l.resolver,
                confidence: l.confidence,
                evidence: l.evidence,
            });
    }
    let mut overall = Sha256::new();
    overall.update(base.as_bytes());
    for (id, g) in &mut out {
        g.edges
            .sort_by(|a, b| a.relationship_id.cmp(&b.relationship_id));
        g.links
            .sort_by(|a, b| a.relationship_id.cmp(&b.relationship_id));
        let mut h = Sha256::new();
        h.update(serde_json::to_vec(&g.edges).unwrap_or_default());
        h.update([0]);
        h.update(serde_json::to_vec(&g.links).unwrap_or_default());
        g.digest = hex(h);
        overall.update(id.as_bytes());
        overall.update([0]);
        overall.update(g.digest.as_bytes());
    }
    Ok(StagedGraph {
        files: out,
        digest: hex(overall),
    })
}

fn hex(h: Sha256) -> String {
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

fn put_graph(doc: &mut serde_json::Map<String, Value>, graph: Value) {
    let versions = doc.entry("versions").or_insert_with(|| json!({}));
    if !versions.is_object() {
        *versions = json!({});
    }
    if let Some(v) = versions.as_object_mut() {
        v.insert(GRAPH_KEY.into(), graph);
    }
}

fn graph_value(doc: &serde_json::Map<String, Value>) -> serde_json::Map<String, Value> {
    doc.get("versions")
        .and_then(|v| v.get(GRAPH_KEY))
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

impl ServerBackend {
    /// The source's graph record (`None` for an unknown source).
    pub fn graph(&self, source_id: &str) -> Result<Option<(ServerGraph, Option<String>)>> {
        Ok(self
            .source_state(source_id)?
            .map(|s| (ServerGraph::from_state(&s), s.active_build_id.clone())))
    }

    /// The graph generation readers may use for `base_build_id`.
    pub fn visible_graph(&self, source_id: &str, base_build_id: &str) -> Result<Option<String>> {
        Ok(self.source_state(source_id)?.and_then(|s| {
            ServerGraph::from_state(&s)
                .visible_generation(Some(base_build_id))
                .map(str::to_owned)
        }))
    }

    /// Records a graph lifecycle change (`disabled`, `failed`, `stale`)
    /// without touching any record.
    pub fn set_graph_state(
        &self,
        source_id: &str,
        lease: Option<&Lease>,
        state: &str,
        error: Option<&str>,
        stale_reason: Option<&str>,
    ) -> Result<()> {
        self.patch_state(source_id, lease, None, |doc, _| {
            let mut g = graph_value(doc);
            g.insert("state".into(), json!(state));
            if error.is_some() || state != "failed" {
                g.insert("last_error".into(), json!(error));
            }
            g.insert("stale_reason".into(), json!(stale_reason));
            put_graph(doc, Value::Object(g));
            Ok(())
        })
    }

    /// Publishes the staged graph as a new generation (see the module docs).
    pub fn publish_graph(&self, input: &GraphPublishInput<'_>) -> Result<GraphPublishReport> {
        let started = Instant::now();
        let mut report = GraphPublishReport::default();
        let prev = self.source_state(input.source_id)?.ok_or_else(|| {
            BackendError::NotFound(format!("no such source: {}", input.source_id))
        })?;
        check_lease(&prev, input.lease)?;
        if prev.active_build_id.as_deref() != Some(input.base_build_id) {
            return Err(BackendError::Conflict(format!(
                "base build {} of {} was superseded; the graph was not published",
                input.base_build_id, input.source_id
            )));
        }
        let current = ServerGraph::from_state(&prev);
        let staged = stage(input.store, input.local_build, input.base_build_id)?;
        let digest = staged.digest.clone();
        if current
            .visible_generation(Some(input.base_build_id))
            .is_some()
            && current.digest.as_deref() == Some(digest.as_str())
        {
            report.generation = current.generation;
            report.seconds = started.elapsed().as_secs_f64();
            return Ok(report);
        }
        // Leftovers of an abandoned pending generation.
        if let Some(old) = current
            .pending
            .as_deref()
            .filter(|p| *p != input.generation)
        {
            self.delete_build_docs(input.source_id, old)?;
        }
        self.patch_state(input.source_id, input.lease, None, |doc, _| {
            let mut g = graph_value(doc);
            g.insert("state".into(), json!("building"));
            g.insert("pending".into(), json!(input.generation));
            put_graph(doc, Value::Object(g));
            Ok(())
        })?;
        let outcome = self
            .write_graph(input, &staged, &current, &mut report)
            .and_then(|()| self.promote_graph(input, &staged));
        match outcome {
            Ok(retired) => {
                for old in retired {
                    // A failure only leaves unreachable records behind; the
                    // next promotion retries.
                    let _ = self.delete_build_docs(input.source_id, &old);
                }
                report.generation = Some(input.generation.to_owned());
                report.published = true;
                report.seconds = started.elapsed().as_secs_f64();
                Ok(report)
            }
            Err(e) => {
                let _ = self.delete_build_docs(input.source_id, input.generation);
                let message = ragmonk_telemetry::redact::redact_urls_in_text(&e.to_string());
                let _ = self.patch_state(input.source_id, input.lease, None, |doc, _| {
                    let mut g = graph_value(doc);
                    if g.get("pending").and_then(Value::as_str) == Some(input.generation) {
                        g.insert("pending".into(), Value::Null);
                    }
                    g.insert("state".into(), json!("failed"));
                    g.insert("last_error".into(), json!(message));
                    put_graph(doc, Value::Object(g));
                    Ok(())
                });
                Err(e)
            }
        }
    }

    /// Writes the pending generation: files whose records equal the
    /// previous generation's are copied forward server-side, in place (the
    /// records gain the new generation id); only the others are written.
    fn write_graph(
        &self,
        input: &GraphPublishInput<'_>,
        staged: &StagedGraph,
        current: &ServerGraph,
        report: &mut GraphPublishReport,
    ) -> Result<()> {
        let (copy, write): (Vec<_>, Vec<_>) = staged.files.iter().partition(|(id, g)| {
            current.generation.is_some() && current.file_digests.get(*id) == Some(&g.digest)
        });
        if let (Some(from), false) = (current.generation.as_deref(), copy.is_empty()) {
            let ids: Vec<String> = copy.iter().map(|(id, _)| (*id).clone()).collect();
            // Generations a reader may still be pinned to keep their ids.
            let mut keep = vec![from.to_owned()];
            keep.extend(current.retired.iter().cloned());
            report.records_copied = self.copy_forward_in(
                &[IndexKind::Relationships],
                input.source_id,
                from,
                input.generation,
                &ids,
                &keep,
            )?;
        }
        report.files_copied = copy.len();
        report.files_written = write.len();
        let mut w = self.build_writer(input.source_id, input.generation);
        for (_, g) in write {
            for e in &g.edges {
                w.edge(e)?;
                report.edges_written += 1;
            }
            for l in &g.links {
                w.link(l)?;
                report.links_written += 1;
            }
        }
        w.finish()?;
        self.refresh_build_indexes()
    }

    /// The fenced compare-and-swap that makes the pending generation the
    /// visible one. Returns retired generations whose grace window ended.
    fn promote_graph(
        &self,
        input: &GraphPublishInput<'_>,
        staged: &StagedGraph,
    ) -> Result<Vec<String>> {
        let digest = staged.digest.as_str();
        let file_digests: serde_json::Map<String, Value> = staged
            .files
            .iter()
            .map(|(id, g)| (id.clone(), json!(g.digest)))
            .collect();
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let grace = self.gc_grace().as_millis();
        let mut expired = Vec::new();
        self.patch_state(input.source_id, input.lease, None, |doc, p| {
            let active = p.and_then(|p| p.active_build_id.as_deref());
            if active != Some(input.base_build_id) {
                return Err(BackendError::Conflict(format!(
                    "base build {} of {} was superseded during the graph build",
                    input.base_build_id, input.source_id
                )));
            }
            let g = graph_value(doc);
            if g.get("pending").and_then(Value::as_str) != Some(input.generation) {
                return Err(BackendError::Conflict(format!(
                    "graph generation {} is not the pending graph of {}",
                    input.generation, input.source_id
                )));
            }
            let mut retired: Vec<(String, u128)> = g
                .get("retired")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|r| {
                    Some((
                        r["generation"].as_str()?.to_owned(),
                        r["retired_at"].as_str()?.parse().ok()?,
                    ))
                })
                .collect();
            if let Some(old) = g
                .get("generation")
                .and_then(Value::as_str)
                .filter(|o| *o != input.generation)
            {
                retired.push((old.to_owned(), now_ms));
            }
            let (keep, gone): (Vec<_>, Vec<_>) = retired
                .into_iter()
                .partition(|(_, at)| grace > 0 && now_ms.saturating_sub(*at) < grace);
            expired = gone.into_iter().map(|(g, _)| g).collect();
            put_graph(
                doc,
                json!({
                    "state": "ready",
                    "generation": input.generation,
                    "base_build_id": input.base_build_id,
                    "digest": digest,
                    "input_digest": input.input_digest,
                    "file_digests": file_digests,
                    "pending": null,
                    "published_at": crate::backend::now(),
                    "last_error": null,
                    "stale_reason": null,
                    "retired": keep.iter().map(|(g, at)| json!({
                        "generation": g, "retired_at": at.to_string()
                    })).collect::<Vec<_>>(),
                }),
            );
            Ok(())
        })?;
        Ok(expired)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(graph: Value, active: Option<&str>) -> SourceState {
        SourceState {
            source_id: "s".into(),
            path: None,
            state: "ready".into(),
            active_build_id: active.map(str::to_owned),
            pending_build_id: None,
            versions: Value::Null,
            doc: json!({ "versions": { "graph": graph } }),
            seq_no: 0,
            primary_term: 1,
        }
    }

    #[test]
    fn only_a_ready_graph_of_the_active_base_is_visible() {
        let st = state(
            json!({"state": "ready", "generation": "g1", "base_build_id": "b1"}),
            Some("b1"),
        );
        let g = ServerGraph::from_state(&st);
        assert_eq!(g.visible_generation(Some("b1")), Some("g1"));
        assert_eq!(g.effective_state(Some("b1")), "ready");
        assert_eq!(g.visible_generation(Some("b2")), None);
        assert_eq!(g.effective_state(Some("b2")), "stale");
        let failed = ServerGraph::from_state(&state(
            json!({"state": "failed", "generation": "g1", "base_build_id": "b1"}),
            Some("b1"),
        ));
        assert_eq!(failed.visible_generation(Some("b1")), None);
        let missing = ServerGraph::from_state(&state(Value::Null, Some("b1")));
        assert_eq!(missing.state, "pending");
        assert_eq!(missing.visible_generation(Some("b1")), None);
    }

    #[test]
    fn graph_record_lives_in_versions() {
        let mut doc = serde_json::Map::new();
        put_graph(&mut doc, json!({"state": "disabled"}));
        assert_eq!(doc["versions"]["graph"]["state"], "disabled");
        assert_eq!(graph_value(&doc)["state"], "disabled");
    }
}
