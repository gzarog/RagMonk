//! Server graph generations through a deterministic in-memory engine:
//! invisible writes, one fenced promotion, refusal against a superseded
//! base build, and cleanup of failed generations. The base build pointer
//! is never touched by a graph publication.

use std::sync::{Arc, Mutex};

use ragmonk_backends::bulk::BulkLimits;
use ragmonk_backends::engine::Engine;
use ragmonk_backends::error::Result as BResult;
use ragmonk_backends::graph::{GraphPublishInput, ServerGraph};
use ragmonk_backends::transport::{Counting, Method, Response, Transport};
use ragmonk_backends::{BackendError, ServerBackend};
use ragmonk_storage::control::IndexVersions;
use ragmonk_storage::knowledge::{FileKnowledge, FileRow, ProjectStore, RelationshipRow};
use ragmonk_storage::StorageLayout;
use serde_json::{json, Value};

#[derive(Default)]
struct Engine0 {
    state: Value,
    seq: i64,
    /// `(build_id, record_kind)` of every record.
    records: Vec<(String, String)>,
    fail_bulk: bool,
    /// Publishes another base build when the graph records are refreshed
    /// (a concurrent base publication between write and promotion).
    switch_base_on_refresh: Option<String>,
}

#[derive(Clone, Default)]
struct Mock(Arc<Mutex<Engine0>>);

fn ok(v: Value) -> BResult<Response> {
    Ok(Response {
        status: 200,
        body: serde_json::to_vec(&v).unwrap(),
    })
}

impl Transport for Mock {
    fn send(&self, method: Method, path: &str, body: Option<(&[u8], &str)>) -> BResult<Response> {
        let mut e = self.0.lock().unwrap();
        let base = path.split('?').next().unwrap();
        if base.starts_with("/rm-source-state/_doc/") {
            if method == Method::Get {
                return ok(json!({"_source": e.state, "_seq_no": e.seq, "_primary_term": 1}));
            }
            let want = format!("if_seq_no={}", e.seq);
            if !path.contains(&want) {
                return Ok(Response {
                    status: 409,
                    body: b"conflict".to_vec(),
                });
            }
            e.state = serde_json::from_slice(body.unwrap().0).unwrap();
            e.seq += 1;
            return ok(json!({"result": "updated"}));
        }
        if base == "/_bulk" {
            if e.fail_bulk {
                return Ok(Response {
                    status: 500,
                    body: b"bulk down".to_vec(),
                });
            }
            let text = String::from_utf8(body.unwrap().0.to_vec()).unwrap();
            for line in text.lines().filter(|l| !l.is_empty()) {
                let v: Value = serde_json::from_str(line).unwrap();
                if let (Some(b), Some(k)) = (v["build_id"].as_str(), v["record_kind"].as_str()) {
                    e.records.push((b.to_owned(), k.to_owned()));
                }
            }
            return ok(json!({"errors": false, "items": []}));
        }
        if base.ends_with("/_refresh") {
            if let Some(b) = e.switch_base_on_refresh.take() {
                e.state["active_build_id"] = json!(b);
                e.seq += 1;
            }
            return ok(json!({}));
        }
        if base.ends_with("/_update_by_query") {
            let v: Value = serde_json::from_slice(body.unwrap().0).unwrap();
            let gone = v["script"]["params"]["b"].as_str().unwrap().to_owned();
            let before = e.records.len();
            e.records.retain(|(b, _)| *b != gone);
            return ok(json!({"deleted": before - e.records.len(), "failures": []}));
        }
        panic!("unexpected request {method:?} {path}");
    }
}

fn backend(m: &Mock) -> ServerBackend {
    ServerBackend::with_client(
        Counting::new(Box::new(m.clone())),
        Engine::OpenSearch,
        "rm",
        None,
        BulkLimits {
            max_actions: 100,
            max_bytes: 1 << 20,
            max_retries: 0,
            base_backoff: std::time::Duration::ZERO,
        },
    )
}

/// A staged store with one published build holding 3 edges.
fn staged(dir: &std::path::Path) -> (ProjectStore, String) {
    let layout = StorageLayout::at(dir);
    let mut s = ProjectStore::open(&layout, "p", "s1", 8).unwrap();
    let build = "local-1".to_owned();
    let v = IndexVersions {
        parser_version: "p".into(),
        chunker_version: "c".into(),
        converter_version: "v".into(),
        embedding_model_id: None,
        embedding_text_version: None,
    };
    s.create_build(&build, true, &v).unwrap();
    let file = FileRow {
        id: "f1".into(),
        rel_path: "a.py".into(),
        kind: "code".into(),
        status: "indexed".into(),
        ..FileRow::default()
    };
    s.put_files(&build, &[(&file, &FileKnowledge::default())])
        .unwrap();
    let rows: Vec<RelationshipRow> = (0..3)
        .map(|i| RelationshipRow {
            id: format!("r{i}"),
            file_id: "f1".into(),
            relationship_type: "calls".into(),
            source_entity_id: "e".into(),
            resolver: "same_file_name".into(),
            confidence: "exact".into(),
            ..RelationshipRow::default()
        })
        .collect();
    s.put_relationships(&build, &rows).unwrap();
    s.mark_published(&build).unwrap();
    (s, build)
}

fn input<'a>(
    store: &'a ProjectStore,
    local: &'a str,
    base: &'a str,
    generation: &'a str,
) -> GraphPublishInput<'a> {
    GraphPublishInput {
        source_id: "s1",
        store,
        local_build: local,
        base_build_id: base,
        lease: None,
        generation,
    }
}

fn setup() -> (Mock, ServerBackend, tempfile::TempDir) {
    let m = Mock::default();
    m.0.lock().unwrap().state = json!({
        "source_id": "s1", "state": "ready", "active_build_id": "b1",
        "versions": { "parser_version": "p" },
    });
    let b = backend(&m);
    (m, b, tempfile::tempdir().unwrap())
}

fn edges_of(m: &Mock, generation: &str) -> usize {
    m.0.lock()
        .unwrap()
        .records
        .iter()
        .filter(|(b, k)| b == generation && k == "edge")
        .count()
}

#[test]
fn a_graph_generation_is_promoted_atomically_without_touching_the_base() {
    let (m, b, dir) = setup();
    let (store, local) = staged(dir.path());
    assert_eq!(b.visible_graph("s1", "b1").unwrap(), None);
    let r = b.publish_graph(&input(&store, &local, "b1", "g1")).unwrap();
    assert!(r.published);
    assert_eq!(r.edges_written, 3);
    assert_eq!(edges_of(&m, "g1"), 3);
    assert_eq!(b.visible_graph("s1", "b1").unwrap().as_deref(), Some("g1"));
    let st = m.0.lock().unwrap().state.clone();
    assert_eq!(st["active_build_id"], "b1", "base pointer untouched");
    assert_eq!(st["versions"]["parser_version"], "p");

    // Same graph again: nothing written, no new generation.
    let r = b.publish_graph(&input(&store, &local, "b1", "g2")).unwrap();
    assert!(!r.published);
    assert_eq!(r.generation.as_deref(), Some("g1"));
    assert_eq!(edges_of(&m, "g2"), 0);

    // A new generation replaces (and garbage-collects) the old one.
    let mut store = store;
    store
        .put_relationships(
            &local,
            &[RelationshipRow {
                id: "r9".into(),
                file_id: "f1".into(),
                relationship_type: "calls".into(),
                source_entity_id: "e".into(),
                resolver: "same_file_name".into(),
                confidence: "exact".into(),
                ..RelationshipRow::default()
            }],
        )
        .unwrap();
    let r = b.publish_graph(&input(&store, &local, "b1", "g3")).unwrap();
    assert!(r.published);
    assert_eq!(b.visible_graph("s1", "b1").unwrap().as_deref(), Some("g3"));
    assert_eq!(edges_of(&m, "g1"), 0, "retired generation collected");
    assert_eq!(edges_of(&m, "g3"), 4);
}

#[test]
fn a_graph_of_a_superseded_base_is_never_promoted() {
    let (m, b, dir) = setup();
    let (store, local) = staged(dir.path());
    // The base moved on before the publication started.
    m.0.lock().unwrap().state["active_build_id"] = json!("b2");
    let e = b
        .publish_graph(&input(&store, &local, "b1", "g1"))
        .unwrap_err();
    assert!(matches!(e, BackendError::Conflict(_)), "{e}");
    assert_eq!(edges_of(&m, "g1"), 0);

    // The base moves on between the write and the promotion.
    m.0.lock().unwrap().state["active_build_id"] = json!("b1");
    m.0.lock().unwrap().switch_base_on_refresh = Some("b3".into());
    let e = b
        .publish_graph(&input(&store, &local, "b1", "g2"))
        .unwrap_err();
    assert!(matches!(e, BackendError::Conflict(_)), "{e}");
    assert_eq!(
        edges_of(&m, "g2"),
        0,
        "the unpromoted generation is removed"
    );
    assert_eq!(b.visible_graph("s1", "b3").unwrap(), None);
    assert_eq!(b.visible_graph("s1", "b1").unwrap(), None);
}

#[test]
fn a_failed_generation_is_never_visible_and_a_republished_base_makes_the_graph_stale() {
    let (m, b, dir) = setup();
    let (store, local) = staged(dir.path());
    b.publish_graph(&input(&store, &local, "b1", "g1")).unwrap();

    m.0.lock().unwrap().fail_bulk = true;
    let mut store = store;
    store
        .put_relationships(
            &local,
            &[RelationshipRow {
                id: "r7".into(),
                file_id: "f1".into(),
                relationship_type: "calls".into(),
                source_entity_id: "e".into(),
                resolver: "same_file_name".into(),
                confidence: "exact".into(),
                ..RelationshipRow::default()
            }],
        )
        .unwrap();
    assert!(b.publish_graph(&input(&store, &local, "b1", "g2")).is_err());
    let (g, active) = b.graph("s1").unwrap().unwrap();
    assert_eq!(g.state, "failed");
    assert!(g.last_error.is_some());
    assert_eq!(b.visible_graph("s1", "b1").unwrap(), None);
    assert_eq!(edges_of(&m, "g2"), 0);
    assert_eq!(active.as_deref(), Some("b1"));

    // Recovery, then a base publication: the graph reports stale.
    m.0.lock().unwrap().fail_bulk = false;
    b.publish_graph(&input(&store, &local, "b1", "g3")).unwrap();
    m.0.lock().unwrap().state["active_build_id"] = json!("b2");
    let (g, active): (ServerGraph, _) = b.graph("s1").unwrap().unwrap();
    assert_eq!(g.effective_state(active.as_deref()), "stale");
    assert_eq!(b.visible_graph("s1", "b2").unwrap(), None);
}
