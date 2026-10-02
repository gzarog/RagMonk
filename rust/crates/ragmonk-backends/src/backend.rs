//! The V2 server backend: schema init, build-scoped writes, atomic
//! publication, visible-build reads and cleanup.
//!
//! Publication protocol per source:
//! 1. `begin_build` records `pending_build_id` in the source-state index
//!    (and deletes the leftovers of any earlier abandoned pending build).
//! 2. Records are bulk-written with `build_id = <pending>` and `_id =
//!    "<build_id>:<record id>"` (deterministic and idempotent within V2).
//! 3. `publish_build` refreshes the build-scoped indexes, then switches
//!    `active_build_id` with an optimistic-concurrency write
//!    (`if_seq_no`/`if_primary_term`), then garbage-collects other builds.
//! 4. Every read filters to `(source_id, active_build_id)` pairs, so a
//!    pending or failed build is never visible.

use std::collections::BTreeMap;
use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};

use crate::bulk::{Action, BulkLimits, BulkReport, BulkWriter};
use crate::engine::{Engine, VectorSpec};
use crate::error::{BackendError, Result};
use crate::schema::{self, IndexKind, IndexSettings};
use crate::transport::{Auth, Client, Counting, HttpTransport, Method, RequestStats, Response};

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct InitReport {
    pub created: Vec<String>,
    pub existing: Vec<String>,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SourceState {
    pub source_id: String,
    pub path: Option<String>,
    pub state: String,
    pub active_build_id: Option<String>,
    pub pending_build_id: Option<String>,
    pub versions: Value,
    seq_no: i64,
    primary_term: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct SearchHit {
    pub id: String,
    pub source_id: String,
    pub rel_path: String,
    pub score: f64,
}

pub struct ServerBackend {
    client: Client,
    engine: Engine,
    prefix: String,
    vector: Option<VectorSpec>,
    limits: BulkLimits,
    settings: IndexSettings,
}

fn ok(resp: Response, method: &str, path: &str) -> Result<Value> {
    if resp.status >= 300 {
        return Err(BackendError::Http {
            method: method.into(),
            path: path.into(),
            status: resp.status,
            reason: String::from_utf8_lossy(&resp.body)
                .chars()
                .take(400)
                .collect(),
        });
    }
    resp.json()
}

fn body(v: &Value) -> Vec<u8> {
    serde_json::to_vec(v).unwrap_or_default()
}

impl ServerBackend {
    /// Connects using `storage.server` config, detecting the engine and
    /// refusing a cluster that does not match the configured engine.
    pub fn connect(
        cfg: &ragmonk_config::model::ServerStorageConfig,
        vector: Option<VectorSpec>,
    ) -> Result<Self> {
        if cfg.url.is_empty() {
            return Err(BackendError::Invalid("storage.server.url is empty".into()));
        }
        let engine_name = cfg.engine.as_str();
        let transport = HttpTransport::new(
            &cfg.url,
            Auth::from_env(engine_name),
            Duration::from_secs_f64(cfg.request_timeout_seconds.max(0.001)),
            cfg.verify_tls,
        )?;
        let client: Client = Counting::new(Box::new(transport));
        let root = ok(client.send(Method::Get, "/", None)?, "GET", "/")?;
        let engine = Engine::detect(&root)
            .ok_or_else(|| BackendError::Invalid("could not detect the server engine".into()))?;
        if engine.as_str() != engine_name {
            return Err(BackendError::Invalid(format!(
                "storage.server.engine is {engine_name} but the cluster is {}",
                engine.as_str()
            )));
        }
        Ok(Self::with_client(
            client,
            engine,
            &schema::v2_prefix(&cfg.index_prefix),
            vector,
            BulkLimits::from_config(&cfg.bulk),
        ))
    }

    pub fn with_client(
        client: Client,
        engine: Engine,
        v2_prefix: &str,
        vector: Option<VectorSpec>,
        limits: BulkLimits,
    ) -> Self {
        Self {
            client,
            engine,
            prefix: v2_prefix.to_owned(),
            vector,
            limits,
            settings: IndexSettings::default(),
        }
    }

    pub fn with_settings(mut self, settings: IndexSettings) -> Self {
        self.settings = settings;
        self
    }

    pub fn engine(&self) -> Engine {
        self.engine
    }
    pub fn prefix(&self) -> &str {
        &self.prefix
    }
    pub fn stats(&self) -> RequestStats {
        self.client.stats()
    }
    pub fn client(&self) -> &Client {
        &self.client
    }
    pub fn index(&self, kind: IndexKind) -> String {
        schema::index_name(&self.prefix, kind)
    }

    fn call(&self, method: Method, path: &str, payload: Option<&Value>) -> Result<Response> {
        match payload {
            Some(v) => {
                let b = body(v);
                self.client
                    .send(method, path, Some((&b, "application/json")))
            }
            None => self.client.send(method, path, None),
        }
    }

    fn call_ok(&self, method: Method, path: &str, payload: Option<&Value>) -> Result<Value> {
        let m = format!("{method:?}").to_uppercase();
        ok(self.call(method, path, payload)?, &m, path)
    }

    // ---- schema --------------------------------------------------------

    /// Creates missing V2 indexes and verifies existing ones. Never
    /// modifies an existing index.
    pub fn init(&self) -> Result<InitReport> {
        let mut report = InitReport {
            created: vec![],
            existing: vec![],
        };
        for kind in IndexKind::ALL {
            let name = self.index(kind);
            let head = self.call(Method::Head, &format!("/{name}"), None)?;
            if head.status == 404 {
                let create =
                    schema::create_body(kind, self.engine, self.vector.as_ref(), &self.settings);
                let resp = self.call(Method::Put, &format!("/{name}"), Some(&create))?;
                let raced = resp.status == 400
                    && String::from_utf8_lossy(&resp.body)
                        .contains("resource_already_exists_exception");
                if !raced {
                    ok(resp, "PUT", &format!("/{name}"))?;
                    report.created.push(name);
                    continue;
                }
            }
            self.verify(kind)?;
            report.existing.push(name);
        }
        self.wait_for_shards()?;
        Ok(report)
    }

    /// Waits until every V2 index has its primaries allocated (yellow), so
    /// the first write/read after creation does not hit an unassigned shard.
    fn wait_for_shards(&self) -> Result<()> {
        let names: Vec<String> = IndexKind::ALL.iter().map(|k| self.index(*k)).collect();
        let path = format!(
            "/_cluster/health/{}?wait_for_status=yellow&timeout=60s",
            names.join(",")
        );
        let v = self.call_ok(Method::Get, &path, None)?;
        if v["timed_out"].as_bool() == Some(true) {
            return Err(BackendError::Transport(format!(
                "V2 indexes did not become available (cluster health {}); check cluster disk/allocation",
                v["status"]
            )));
        }
        Ok(())
    }

    fn verify(&self, kind: IndexKind) -> Result<()> {
        let name = self.index(kind);
        let mapping = self.call_ok(Method::Get, &format!("/{name}/_mapping"), None)?;
        let meta = mapping
            .as_object()
            .and_then(|m| m.values().next())
            .and_then(|v| v.pointer("/mappings/_meta"));
        schema::check_meta(&name, meta, kind, self.vector.as_ref())
            .map_err(BackendError::SchemaMismatch)
    }

    // ---- source state ---------------------------------------------------

    pub fn source_state(&self, source_id: &str) -> Result<Option<SourceState>> {
        let path = format!(
            "/{}/_doc/{}",
            self.index(IndexKind::SourceState),
            encode_id(source_id)
        );
        let resp = self.call(Method::Get, &path, None)?;
        if resp.status == 404 {
            return Ok(None);
        }
        let v = ok(resp, "GET", &path)?;
        let src = &v["_source"];
        let s = |k: &str| src.get(k).and_then(Value::as_str).map(str::to_owned);
        Ok(Some(SourceState {
            source_id: source_id.to_owned(),
            path: s("path"),
            state: s("state").unwrap_or_default(),
            active_build_id: s("active_build_id"),
            pending_build_id: s("pending_build_id"),
            versions: src.get("versions").cloned().unwrap_or(Value::Null),
            seq_no: v["_seq_no"].as_i64().unwrap_or(-1),
            primary_term: v["_primary_term"].as_i64().unwrap_or(-1),
        }))
    }

    fn write_state(&self, prev: Option<&SourceState>, doc: &Value) -> Result<()> {
        let source_id = doc["source_id"].as_str().unwrap_or_default();
        let index = self.index(IndexKind::SourceState);
        let path = match prev {
            Some(p) => format!(
                "/{index}/_doc/{}?if_seq_no={}&if_primary_term={}&refresh=true",
                encode_id(source_id),
                p.seq_no,
                p.primary_term
            ),
            None => format!("/{index}/_create/{}?refresh=true", encode_id(source_id)),
        };
        let resp = self.call(Method::Put, &path, Some(doc))?;
        if resp.status == 409 {
            return Err(BackendError::Conflict(format!(
                "source {source_id} state changed concurrently; another RagMonk process is publishing it"
            )));
        }
        ok(resp, "PUT", &path).map(|_| ())
    }

    /// Starts a build. Leftovers of a previous abandoned pending build are
    /// deleted first; the active build stays visible throughout.
    pub fn begin_build(&self, source_id: &str, path: &str, build_id: &str) -> Result<()> {
        let prev = self.source_state(source_id)?;
        if let Some(old) = prev.as_ref().and_then(|p| p.pending_build_id.clone()) {
            if old != build_id {
                self.delete_build_docs(source_id, &old)?;
            }
        }
        let now = now();
        let active = prev.as_ref().and_then(|p| p.active_build_id.clone());
        let doc = json!({
            "source_id": source_id,
            "path": path,
            "state": "building",
            "active_build_id": active,
            "pending_build_id": build_id,
            "versions": prev.as_ref().map(|p| p.versions.clone()).unwrap_or(Value::Null),
            "last_full_build_at": Value::Null,
            "updated_at": now,
        });
        self.write_state(prev.as_ref(), &doc)
    }

    /// Writer for records of one build.
    pub fn build_writer(&self, source_id: &str, build_id: &str) -> BuildWriter<'_> {
        BuildWriter {
            backend: self,
            source_id: source_id.to_owned(),
            build_id: build_id.to_owned(),
            bulk: BulkWriter::new(&self.client, self.limits),
        }
    }

    fn refresh_build_indexes(&self) -> Result<()> {
        let names: Vec<String> = IndexKind::BUILD_SCOPED
            .iter()
            .map(|k| self.index(*k))
            .collect();
        self.call_ok(
            Method::Post,
            &format!("/{}/_refresh", names.join(",")),
            None,
        )
        .map(|_| ())
    }

    /// Atomically makes `build_id` the visible build, then removes every
    /// other build of the source.
    pub fn publish_build(
        &self,
        source_id: &str,
        build_id: &str,
        versions: &Value,
        full: bool,
    ) -> Result<()> {
        let prev = self
            .source_state(source_id)?
            .ok_or_else(|| BackendError::Invalid(format!("no build started for {source_id}")))?;
        if prev.pending_build_id.as_deref() != Some(build_id) {
            return Err(BackendError::Conflict(format!(
                "build {build_id} is not the pending build of {source_id}"
            )));
        }
        self.refresh_build_indexes()?;
        let now = now();
        let last_full = if full {
            Value::String(now.clone())
        } else {
            Value::Null
        };
        let doc = json!({
            "source_id": source_id,
            "path": prev.path,
            "state": "ready",
            "active_build_id": build_id,
            "pending_build_id": Value::Null,
            "versions": versions,
            "last_full_build_at": last_full,
            "updated_at": now,
        });
        self.write_state(Some(&prev), &doc)?;
        self.gc_source(source_id, build_id)
    }

    /// Discards a pending build. The previously active build (if any)
    /// stays visible.
    pub fn abort_build(&self, source_id: &str, build_id: &str) -> Result<()> {
        self.delete_build_docs(source_id, build_id)?;
        if let Some(prev) = self.source_state(source_id)? {
            if prev.pending_build_id.as_deref() == Some(build_id) {
                let doc = json!({
                    "source_id": source_id,
                    "path": prev.path,
                    "state": if prev.active_build_id.is_some() { "failed" } else { "needs_full_rebuild" },
                    "active_build_id": prev.active_build_id,
                    "pending_build_id": Value::Null,
                    "versions": prev.versions,
                    "last_full_build_at": Value::Null,
                    "updated_at": now(),
                });
                self.write_state(Some(&prev), &doc)?;
            }
        }
        Ok(())
    }

    fn delete_by_query(&self, query: &Value) -> Result<u64> {
        let names: Vec<String> = IndexKind::BUILD_SCOPED
            .iter()
            .map(|k| self.index(*k))
            .collect();
        let path = format!(
            "/{}/_delete_by_query?conflicts=proceed&refresh=true&wait_for_completion=true",
            names.join(",")
        );
        let v = self.call_ok(Method::Post, &path, Some(&json!({ "query": query })))?;
        Ok(v["deleted"].as_u64().unwrap_or(0))
    }

    fn delete_build_docs(&self, source_id: &str, build_id: &str) -> Result<u64> {
        self.delete_by_query(&json!({ "bool": { "filter": [
            { "term": { "source_id": source_id } },
            { "term": { "build_id": build_id } },
        ]}}))
    }

    fn gc_source(&self, source_id: &str, keep_build: &str) -> Result<()> {
        self.delete_by_query(&json!({ "bool": {
            "filter": [ { "term": { "source_id": source_id } } ],
            "must_not": [ { "term": { "build_id": keep_build } } ],
        }}))
        .map(|_| ())
    }

    /// Deletes every record and the state of a source.
    pub fn remove_source(&self, source_id: &str) -> Result<u64> {
        let n = self.delete_by_query(&json!({ "term": { "source_id": source_id } }))?;
        let path = format!(
            "/{}/_doc/{}?refresh=true",
            self.index(IndexKind::SourceState),
            encode_id(source_id)
        );
        let resp = self.call(Method::Delete, &path, None)?;
        if resp.status != 404 {
            ok(resp, "DELETE", &path)?;
        }
        Ok(n)
    }

    // ---- reads ----------------------------------------------------------

    /// `source_id -> active_build_id` for every published source.
    pub fn active_builds(&self) -> Result<BTreeMap<String, String>> {
        let path = format!("/{}/_search", self.index(IndexKind::SourceState));
        let v = self.call_ok(
            Method::Post,
            &path,
            Some(&json!({
                "size": 10000,
                "_source": ["source_id", "active_build_id"],
                "query": { "exists": { "field": "active_build_id" } }
            })),
        )?;
        let mut out = BTreeMap::new();
        for hit in v
            .pointer("/hits/hits")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let src = &hit["_source"];
            if let (Some(s), Some(b)) = (src["source_id"].as_str(), src["active_build_id"].as_str())
            {
                out.insert(s.to_owned(), b.to_owned());
            }
        }
        Ok(out)
    }

    /// Filter that only matches records of each source's active build.
    pub fn visibility_filter(active: &BTreeMap<String, String>) -> Value {
        if active.is_empty() {
            return json!({ "match_none": {} });
        }
        let should: Vec<Value> = active
            .iter()
            .map(|(s, b)| {
                json!({ "bool": { "filter": [
                    { "term": { "source_id": s } },
                    { "term": { "build_id": b } },
                ]}})
            })
            .collect();
        json!({ "bool": { "should": should, "minimum_should_match": 1 } })
    }

    fn search(
        &self,
        kind: IndexKind,
        id_field: &str,
        query: Value,
        limit: usize,
    ) -> Result<Vec<SearchHit>> {
        let active = self.active_builds()?;
        let path = format!("/{}/_search", self.index(kind));
        let v = self.call_ok(
            Method::Post,
            &path,
            Some(&json!({
                "size": limit,
                "_source": [id_field, "source_id", "rel_path"],
                "query": { "bool": {
                    "must": [ query ],
                    "filter": [ Self::visibility_filter(&active) ],
                }},
            })),
        )?;
        Ok(v.pointer("/hits/hits")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(|h| SearchHit {
                id: h["_source"][id_field]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                source_id: h["_source"]["source_id"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                rel_path: h["_source"]["rel_path"]
                    .as_str()
                    .unwrap_or_default()
                    .to_owned(),
                score: h["_score"].as_f64().unwrap_or(0.0),
            })
            .collect())
    }

    /// Basic lexical chunk search over the visible builds (ranking/fusion
    /// is RUST-10 scope).
    pub fn search_chunks(&self, text: &str, limit: usize) -> Result<Vec<SearchHit>> {
        self.search(
            IndexKind::Chunks,
            "chunk_id",
            json!({ "multi_match": { "query": text, "fields": ["search_text", "heading_path", "title", "caption"] } }),
            limit,
        )
    }

    pub fn search_code(&self, text: &str, limit: usize) -> Result<Vec<SearchHit>> {
        self.search(
            IndexKind::Code,
            "entity_id",
            json!({ "multi_match": { "query": text, "fields": ["name^3", "qualified_name^2", "signature"] } }),
            limit,
        )
    }

    /// Visible record count of one index (optionally one source).
    pub fn count(&self, kind: IndexKind, source_id: Option<&str>) -> Result<u64> {
        let active = self.active_builds()?;
        let mut filter = vec![Self::visibility_filter(&active)];
        if let Some(s) = source_id {
            filter.push(json!({ "term": { "source_id": s } }));
        }
        let v = self.call_ok(
            Method::Post,
            &format!("/{}/_count", self.index(kind)),
            Some(&json!({ "query": { "bool": { "filter": filter } } })),
        )?;
        Ok(v["count"].as_u64().unwrap_or(0))
    }

    /// Raw record count including invisible builds (diagnostics/tests).
    pub fn raw_count(&self, kind: IndexKind, source_id: &str, build_id: &str) -> Result<u64> {
        let v = self.call_ok(
            Method::Post,
            &format!("/{}/_count", self.index(kind)),
            Some(&json!({ "query": { "bool": { "filter": [
                { "term": { "source_id": source_id } },
                { "term": { "build_id": build_id } },
            ]}}})),
        )?;
        Ok(v["count"].as_u64().unwrap_or(0))
    }

    // ---- build-mode tuning -------------------------------------------------

    /// Disables refresh and replicas on the build-scoped indexes for a
    /// large rebuild. Restore with [`BuildMode::restore`]. Only use while
    /// no other process serves searches from this cluster's V2 indexes.
    pub fn enter_build_mode(&self) -> Result<BuildMode<'_>> {
        let mut saved = Vec::new();
        for kind in IndexKind::BUILD_SCOPED {
            let name = self.index(kind);
            let v = self.call_ok(
                Method::Get,
                &format!("/{name}/_settings?include_defaults=true&flat_settings=true"),
                None,
            )?;
            let s = v
                .as_object()
                .and_then(|m| m.values().next())
                .cloned()
                .unwrap_or_default();
            let get = |k: &str| {
                s.pointer(&format!("/settings/{}", k.replace('/', "~1")))
                    .or_else(|| s.pointer(&format!("/defaults/{}", k.replace('/', "~1"))))
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            };
            saved.push((
                name.clone(),
                get("index.refresh_interval").unwrap_or_else(|| "1s".into()),
                get("index.number_of_replicas")
                    .unwrap_or_else(|| self.settings.replicas.to_string()),
            ));
            self.call_ok(
                Method::Put,
                &format!("/{name}/_settings"),
                Some(&json!({ "index": { "refresh_interval": "-1", "number_of_replicas": 0 } })),
            )?;
        }
        Ok(BuildMode {
            backend: self,
            saved,
            restored: false,
        })
    }
}

/// Saved serving settings; restored explicitly or (best effort) on drop.
pub struct BuildMode<'a> {
    backend: &'a ServerBackend,
    saved: Vec<(String, String, String)>,
    restored: bool,
}

impl BuildMode<'_> {
    pub fn restore(mut self) -> Result<()> {
        self.restore_inner()
    }

    fn restore_inner(&mut self) -> Result<()> {
        if self.restored {
            return Ok(());
        }
        for (name, refresh, replicas) in &self.saved {
            self.backend.call_ok(
                Method::Put,
                &format!("/{name}/_settings"),
                Some(&json!({ "index": { "refresh_interval": refresh, "number_of_replicas": replicas } })),
            )?;
        }
        self.restored = true;
        Ok(())
    }
}

impl Drop for BuildMode<'_> {
    fn drop(&mut self) {
        let _ = self.restore_inner();
    }
}

/// Typed records written into a build. Field names match the V2 mappings.
#[derive(Debug, Clone, Serialize)]
pub struct FileDoc {
    pub file_id: String,
    pub rel_path: String,
    pub kind: String,
    pub size: i64,
    pub mtime: f64,
    pub content_hash: Option<String>,
    pub parser_version: Option<String>,
    pub chunker_version: Option<String>,
    pub converter_version: Option<String>,
    pub embedding_model_id: Option<String>,
    pub embedding_text_version: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EntityDoc {
    pub entity_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub language: String,
    pub parent_id: Option<String>,
    pub signature: Option<String>,
    pub start_line: i64,
    pub end_line: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct DocumentDoc {
    pub document_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub format: String,
    pub title: Option<String>,
    pub author: Option<String>,
    pub page_count: Option<i64>,
    pub is_scanned: bool,
    pub content_hash: Option<String>,
    pub parent_document_id: Option<String>,
    pub attachment_name: Option<String>,
    pub attachment_content_type: Option<String>,
    pub attachment_index: Option<i64>,
    pub attachment_content_id: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChunkDoc {
    pub chunk_id: String,
    pub document_id: String,
    pub parent_document_id: Option<String>,
    pub file_id: String,
    pub rel_path: String,
    pub kind: String,
    pub ordinal: i64,
    pub heading_path: Vec<String>,
    pub heading_level: Option<i64>,
    pub title: Option<String>,
    pub search_text: String,
    pub text: String,
    pub embedding_text: Option<String>,
    pub token_count: Option<i64>,
    pub page_start: Option<i64>,
    pub page_end: Option<i64>,
    pub table_rows: Option<Vec<Vec<String>>>,
    pub caption: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub embedding: Option<Vec<f32>>,
}

#[derive(Debug, Clone, Serialize)]
pub struct EdgeDoc {
    pub relationship_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub relationship_type: String,
    pub source_entity_id: String,
    pub target_entity_id: Option<String>,
    pub target_symbol: Option<String>,
    pub resolver: String,
    pub confidence: String,
    pub source_location: Option<String>,
    pub evidence: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct LinkDoc {
    pub relationship_id: String,
    pub relationship_type: String,
    pub entity_id: String,
    pub document_id: String,
    pub chunk_id: Option<String>,
    pub file_id: String,
    pub rel_path: String,
    pub resolver: String,
    pub confidence: String,
    pub evidence: Option<String>,
}

/// Bulk writer bound to one `(source, build)`.
pub struct BuildWriter<'a> {
    backend: &'a ServerBackend,
    source_id: String,
    build_id: String,
    bulk: BulkWriter<'a>,
}

impl BuildWriter<'_> {
    fn put(
        &mut self,
        kind: IndexKind,
        record_id: &str,
        doc: &impl Serialize,
        extra: &[(&str, &str)],
    ) -> Result<()> {
        let mut v = serde_json::to_value(doc).map_err(|e| BackendError::Invalid(e.to_string()))?;
        if let Some(obj) = v.as_object_mut() {
            obj.insert("source_id".into(), Value::String(self.source_id.clone()));
            obj.insert("build_id".into(), Value::String(self.build_id.clone()));
            for (k, val) in extra {
                obj.insert((*k).into(), Value::String((*val).into()));
            }
        }
        self.bulk.push(&Action::Index {
            index: self.backend.index(kind),
            id: format!("{}:{record_id}", self.build_id),
            doc: v,
        })
    }

    pub fn file(&mut self, d: &FileDoc) -> Result<()> {
        let path_text = d.rel_path.replace(['/', '\\', '.', '_', '-'], " ");
        self.put(
            IndexKind::Files,
            &d.file_id.clone(),
            d,
            &[("path_text", &path_text)],
        )
    }
    pub fn entity(&mut self, d: &EntityDoc) -> Result<()> {
        self.put(IndexKind::Code, &d.entity_id.clone(), d, &[])
    }
    pub fn document(&mut self, d: &DocumentDoc) -> Result<()> {
        self.put(IndexKind::Documents, &d.document_id.clone(), d, &[])
    }
    pub fn chunk(&mut self, d: &ChunkDoc) -> Result<()> {
        self.put(IndexKind::Chunks, &d.chunk_id.clone(), d, &[])
    }
    pub fn edge(&mut self, d: &EdgeDoc) -> Result<()> {
        self.put(
            IndexKind::Relationships,
            &d.relationship_id.clone(),
            d,
            &[("record_kind", "edge")],
        )
    }
    pub fn link(&mut self, d: &LinkDoc) -> Result<()> {
        self.put(
            IndexKind::Relationships,
            &d.relationship_id.clone(),
            d,
            &[("record_kind", "link")],
        )
    }

    pub fn finish(self) -> Result<BulkReport> {
        self.bulk.finish()
    }
}

/// Document IDs go into URL paths.
fn encode_id(id: &str) -> String {
    id.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            other => format!("%{other:02X}"),
        })
        .collect()
}

/// Epoch milliseconds; both engines accept `epoch_millis` for `date` fields.
fn now() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn visibility_filter_shapes() {
        assert_eq!(
            ServerBackend::visibility_filter(&BTreeMap::new()),
            json!({"match_none": {}})
        );
        let mut m = BTreeMap::new();
        m.insert("s".to_string(), "b".to_string());
        let f = ServerBackend::visibility_filter(&m);
        assert_eq!(f["bool"]["minimum_should_match"], 1);
        assert_eq!(
            f["bool"]["should"][0]["bool"]["filter"][1]["term"]["build_id"],
            "b"
        );
    }

    #[test]
    fn ids_are_url_safe() {
        assert_eq!(encode_id("src_ab12"), "src_ab12");
        assert_eq!(encode_id("a/b:c"), "a%2Fb%3Ac");
    }
}
