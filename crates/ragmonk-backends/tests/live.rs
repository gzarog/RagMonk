//! Real-cluster acceptance tests for the server index schema.
//!
//! Set `RAGMONK_TEST_OPENSEARCH_URL` / `RAGMONK_TEST_ELASTICSEARCH_URL` to a
//! disposable test cluster. With `RAGMONK_REQUIRE_LIVE_SERVER=1` a missing
//! URL is a failure instead of a skip (CI). The scenario creates and deletes
//! uniquely named indexes, so never point these variables at a real
//! deployment.

use std::time::Duration;

use ragmonk_backends::backend::{ChunkDoc, DocumentDoc, EdgeDoc, EntityDoc, FileDoc, LinkDoc};
use ragmonk_backends::bulk::BulkLimits;
use ragmonk_backends::engine::{Engine, VectorSpec};
use ragmonk_backends::schema::{IndexKind, IndexSettings};
use ragmonk_backends::transport::{Auth, Client, Counting, HttpTransport, Method};
use ragmonk_backends::{BackendError, ServerBackend};
use ragmonk_core::ids::record;
use serde_json::json;

fn url(var: &str) -> Option<String> {
    match std::env::var(var) {
        Ok(u) if !u.is_empty() => Some(u),
        _ => {
            assert!(
                std::env::var("RAGMONK_REQUIRE_LIVE_SERVER").as_deref() != Ok("1"),
                "{var} is required when RAGMONK_REQUIRE_LIVE_SERVER=1"
            );
            eprintln!("skipping: {var} not set");
            None
        }
    }
}

fn client(base: &str) -> Client {
    Counting::new(Box::new(
        HttpTransport::new(base, Auth::None, Duration::from_secs(60), true).unwrap(),
    ))
}

fn unique(tag: &str) -> String {
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!("rmtest{tag}{}", n % 1_000_000_000)
}

fn vector(dims: u32) -> VectorSpec {
    VectorSpec {
        dims,
        m: 16,
        ef_construction: 100,
        model_id: "test-model".into(),
    }
}

fn backend(
    base: &str,
    engine: Engine,
    prefix: &str,
    dims: u32,
    max_actions: usize,
) -> ServerBackend {
    ServerBackend::with_client(
        client(base),
        engine,
        prefix,
        Some(vector(dims)),
        BulkLimits {
            max_actions,
            max_bytes: 5_000_000,
            max_retries: 3,
            base_backoff: Duration::from_millis(50),
        },
    )
    .with_settings(IndexSettings {
        shards: 1,
        replicas: 0,
    })
}

fn delete_index(c: &Client, name: &str) {
    let _ = c.send(Method::Delete, &format!("/{name}"), None);
}

/// Writes a synthetic source with `files` code files and one email with an
/// attachment child document. Returns the number of chunks written.
fn write_corpus(b: &ServerBackend, source: &str, build: &str, files: usize, marker: &str) -> usize {
    let mut w = b.build_writer(source, build);
    let mut chunks = 0;
    for i in 0..files {
        let rel = format!("pkg/mod_{i}.py");
        let file_id = record::file_id(source, &rel);
        w.file(&FileDoc {
            file_id: file_id.clone(),
            rel_path: rel.clone(),
            kind: "code".into(),
            size: 100,
            mtime: 1.0,
            content_hash: Some(format!("h{i}")),
            parser_version: Some("p".into()),
            chunker_version: Some("c".into()),
            converter_version: Some("d".into()),
            embedding_model_id: Some("test-model".into()),
            embedding_text_version: Some("e".into()),
        })
        .unwrap();
        let caller =
            record::entity_id(&file_id, "function", &format!("pkg.mod_{i}.{marker}_fn"), 0);
        let callee = record::entity_id(&file_id, "function", &format!("pkg.mod_{i}.helper"), 0);
        for (id, name) in [
            (&caller, format!("{marker}_fn")),
            (&callee, "helper".to_string()),
        ] {
            w.entity(&EntityDoc {
                entity_id: id.clone(),
                file_id: file_id.clone(),
                rel_path: rel.clone(),
                kind: "function".into(),
                name: name.clone(),
                qualified_name: format!("pkg.mod_{i}.{name}"),
                language: "python".into(),
                parent_id: None,
                signature: Some(format!("def {name}()")),
                start_line: 1,
                end_line: 3,
            })
            .unwrap();
        }
        w.edge(&EdgeDoc {
            relationship_id: record::relationship_id(&caller, "calls", &callee, 0),
            file_id: file_id.clone(),
            rel_path: rel.clone(),
            relationship_type: "calls".into(),
            source_entity_id: caller.clone(),
            target_entity_id: Some(callee.clone()),
            target_symbol: Some("helper".into()),
            resolver: "same_file_name".into(),
            confidence: "exact".into(),
            source_location: Some(format!("{rel}:2")),
            evidence: Some("helper()".into()),
        })
        .unwrap();
    }
    // An email with one attachment child document.
    let rel = "mail/budget.eml".to_string();
    let file_id = record::file_id(source, &rel);
    let parent = record::document_id(&file_id, None);
    let child = record::document_id(&file_id, Some(1));
    for (doc_id, att) in [(&parent, None), (&child, Some(1))] {
        w.document(&DocumentDoc {
            document_id: doc_id.clone(),
            file_id: file_id.clone(),
            rel_path: rel.clone(),
            format: if att.is_some() { "txt" } else { "eml" }.into(),
            title: Some("Budget".into()),
            author: None,
            page_count: None,
            is_scanned: false,
            content_hash: None,
            parent_document_id: att.map(|_| parent.clone()),
            attachment_name: att.map(|_| "notes.txt".into()),
            attachment_content_type: att.map(|_| "text/plain".into()),
            attachment_index: att,
            attachment_content_id: None,
        })
        .unwrap();
        for ord in 0..3u64 {
            let id = record::chunk_id(doc_id, ord);
            w.chunk(&ChunkDoc {
                chunk_id: id.clone(),
                document_id: doc_id.clone(),
                parent_document_id: att.map(|_| parent.clone()),
                file_id: file_id.clone(),
                rel_path: rel.clone(),
                kind: "paragraph".into(),
                ordinal: ord as i64,
                heading_path: vec!["Quarterly".into()],
                heading_level: None,
                title: Some("Budget".into()),
                search_text: format!("{marker} budget review paragraph {ord}"),
                text: format!("{marker} budget review paragraph {ord}"),
                embedding_text: None,
                token_count: Some(5),
                page_start: None,
                page_end: None,
                table_rows: None,
                caption: None,
                embedding: Some(vec![0.5, 0.5, 0.5, 0.5]),
            })
            .unwrap();
            chunks += 1;
        }
    }
    w.link(&LinkDoc {
        relationship_id: record::link_id("e", &parent, None, "mentioned_in", "exact_name"),
        relationship_type: "mentioned_in".into(),
        entity_id: "e".into(),
        document_id: parent.clone(),
        chunk_id: None,
        file_id: file_id.clone(),
        rel_path: rel,
        resolver: "exact_name".into(),
        confidence: "high".into(),
        evidence: None,
    })
    .unwrap();
    w.finish().unwrap();
    chunks
}

fn scenario(base: &str, engine: Engine) {
    let prefix = unique("live");
    let c = client(base);

    // ---- a foreign index under the prefix is refused, never modified -----
    let foreign = unique("foreign");
    let r = c
        .send(
            Method::Put,
            &format!("/{foreign}-files"),
            Some((
                br#"{"mappings":{"properties":{"x":{"type":"keyword"}}}}"#,
                "application/json",
            )),
        )
        .unwrap();
    assert!(r.status < 300, "{}", String::from_utf8_lossy(&r.body));
    let err = backend(base, engine, &foreign, 4, 40).init().unwrap_err();
    assert!(err.to_string().contains("not a RagMonk index"), "{err}");
    let mapping = c
        .send(Method::Get, &format!("/{foreign}-files/_mapping"), None)
        .unwrap();
    let body = String::from_utf8_lossy(&mapping.body);
    assert!(!body.contains("_meta"), "foreign index untouched: {body}");
    for kind in [
        "files",
        "source-state",
        "code",
        "documents",
        "chunks",
        "relationships",
    ] {
        delete_index(&c, &format!("{foreign}-{kind}"));
    }

    // ---- empty init -----------------------------------------------------------
    let b = backend(base, engine, &prefix, 4, 40);
    let init = b.init().unwrap();
    assert_eq!(init.created.len(), 6);
    assert!(b.init().unwrap().existing.len() == 6, "init is idempotent");
    let wrong = backend(base, engine, &prefix, 8, 40);
    assert!(matches!(wrong.init(), Err(BackendError::SchemaMismatch(_))));

    // ---- full rebuild, invisible until published -------------------------
    let src = "src_live";
    let versions = json!({"schema_version": 1, "parser_version": "p"});
    b.begin_build(src, "/repo", "b1").unwrap();
    let before = b.stats();
    let chunks = write_corpus(&b, src, "b1", 120, "alpha");
    let after = b.stats();
    let actions = after.bulk_actions - before.bulk_actions;
    // 4 records per code file, 2 documents, the chunks and 1 link.
    assert_eq!(actions as usize, 120 * 4 + 2 + chunks + 1);
    let bulk_requests = after.bulk_requests - before.bulk_requests;
    assert!(
        bulk_requests as usize <= actions.div_ceil(40) as usize + 2,
        "bounded batches: {bulk_requests}"
    );
    assert_eq!(
        after.requests - before.requests,
        bulk_requests,
        "every record write went through _bulk"
    );
    assert!(
        after.max_bulk_actions <= 40,
        "action bound: {}",
        after.max_bulk_actions
    );
    assert!(
        after.max_bulk_bytes <= 5_000_000,
        "byte bound: {}",
        after.max_bulk_bytes
    );
    b.client()
        .send(Method::Post, &format!("/{prefix}-*/_refresh"), None)
        .unwrap();
    assert_eq!(
        b.raw_count(IndexKind::Chunks, src, "b1").unwrap(),
        chunks as u64
    );
    assert_eq!(
        b.count(IndexKind::Chunks, None).unwrap(),
        0,
        "unpublished build invisible"
    );
    assert!(b.search_chunks("alpha", 10).unwrap().is_empty());

    b.publish_build(src, "b1", &versions, true).unwrap();
    assert_eq!(
        b.count(IndexKind::Chunks, Some(src)).unwrap(),
        chunks as u64
    );
    assert_eq!(b.count(IndexKind::Code, Some(src)).unwrap(), 240);
    assert_eq!(b.count(IndexKind::Documents, Some(src)).unwrap(), 2);
    assert_eq!(b.count(IndexKind::Relationships, Some(src)).unwrap(), 121);
    assert!(!b.search_chunks("alpha budget", 10).unwrap().is_empty());
    assert!(!b.search_code("alpha_fn", 10).unwrap().is_empty());

    // Re-writing the same records into the same build is idempotent.
    b.begin_build(src, "/repo", "b2").unwrap();
    write_corpus(&b, src, "b2", 10, "beta");
    write_corpus(&b, src, "b2", 10, "beta");
    b.client()
        .send(Method::Post, &format!("/{prefix}-*/_refresh"), None)
        .unwrap();
    assert_eq!(b.raw_count(IndexKind::Code, src, "b2").unwrap(), 20);

    // ---- failure injection: b2 "crashes" (never published) --------------
    assert!(
        b.search_code("beta_fn", 10).unwrap().is_empty(),
        "crashed build invisible"
    );
    assert_eq!(
        b.count(IndexKind::Code, Some(src)).unwrap(),
        240,
        "previous build still served"
    );
    // A new build cleans up the abandoned one.
    b.begin_build(src, "/repo", "b3").unwrap();
    assert_eq!(b.raw_count(IndexKind::Code, src, "b2").unwrap(), 0);
    write_corpus(&b, src, "b3", 5, "gamma");
    b.abort_build(src, "b3").unwrap();
    assert_eq!(b.raw_count(IndexKind::Code, src, "b3").unwrap(), 0);
    let state = b.source_state(src).unwrap().unwrap();
    assert_eq!(
        (state.state.as_str(), state.active_build_id.as_deref()),
        ("failed", Some("b1"))
    );
    assert!(matches!(
        b.publish_build(src, "b3", &versions, false),
        Err(BackendError::Conflict(_))
    ));

    // ---- incremental publish garbage-collects the old build --------------
    b.begin_build(src, "/repo", "b4").unwrap();
    write_corpus(&b, src, "b4", 30, "delta");
    b.publish_build(src, "b4", &versions, false).unwrap();
    assert_eq!(b.count(IndexKind::Code, Some(src)).unwrap(), 60);
    assert_eq!(b.raw_count(IndexKind::Code, src, "b1").unwrap(), 0);
    assert!(b.search_code("alpha_fn", 10).unwrap().is_empty());
    assert!(!b.search_code("delta_fn", 10).unwrap().is_empty());

    // ---- build mode toggles and restores serving settings -----------------
    let mode = b.enter_build_mode().unwrap();
    let s = b
        .client()
        .send(
            Method::Get,
            &format!("/{prefix}-chunks/_settings?flat_settings=true"),
            None,
        )
        .unwrap()
        .json()
        .unwrap();
    assert_eq!(
        s.pointer(&format!("/{prefix}-chunks/settings/index.refresh_interval"))
            .unwrap(),
        "-1"
    );
    mode.restore().unwrap();
    let s = b
        .client()
        .send(
            Method::Get,
            &format!("/{prefix}-chunks/_settings?include_defaults=true&flat_settings=true"),
            None,
        )
        .unwrap()
        .json()
        .unwrap();
    let refresh = s
        .pointer(&format!("/{prefix}-chunks/settings/index.refresh_interval"))
        .or_else(|| s.pointer(&format!("/{prefix}-chunks/defaults/index.refresh_interval")))
        .unwrap();
    assert_ne!(refresh, "-1");

    // ---- source removal ---------------------------------------------------
    assert!(b.remove_source(src).unwrap() > 0);
    assert_eq!(b.count(IndexKind::Code, None).unwrap(), 0);
    assert!(b.source_state(src).unwrap().is_none());

    for kind in IndexKind::ALL {
        delete_index(&c, &b.index(kind));
    }
}

#[test]
fn opensearch_lifecycle() {
    if let Some(u) = url("RAGMONK_TEST_OPENSEARCH_URL") {
        scenario(&u, Engine::OpenSearch);
    }
}

#[test]
fn elasticsearch_lifecycle() {
    if let Some(u) = url("RAGMONK_TEST_ELASTICSEARCH_URL") {
        scenario(&u, Engine::Elasticsearch);
    }
}
