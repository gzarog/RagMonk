//! Query commands (RUST-12 slice 2): `search`, `symbol`, `callers`,
//! `callees`, `references`, `impact`, `explore` and `link …`. `--json`
//! payloads match the reference. Text output keeps its content and order
//! as plain text.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use clap::Subcommand;
use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::{project_id_for_canonical, Home};
use ragmonk_retrieval::graph::{self, Direction, SourceMatch, TraversalEdge};
use ragmonk_retrieval::lexical::{self, SearchResult};
use ragmonk_retrieval::{classify, context, explore, hybrid, Corpus};
use ragmonk_storage::control::SourceRecord;
use ragmonk_storage::knowledge::ProjectStore;
use ragmonk_storage::V2Layout;
use serde_json::{json, Value};

use crate::workflow::{control_plane, print_table};
use crate::{load, prepared_home, print_json};

fn db(e: impl std::fmt::Display) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

/// One indexed source: its record, store and active build.
pub struct Opened {
    pub source: SourceRecord,
    pub store: ProjectStore,
    pub build: String,
}

/// Every source with a published build (optionally one source).
pub fn open_sources(home: &Home, only: Option<&str>) -> Result<Vec<Opened>, RagMonkError> {
    let cfg = load(home)?;
    let cp = control_plane(home)?;
    let sources = match only {
        Some(id) => vec![cp
            .get_source(id)
            .map_err(db)?
            .ok_or_else(|| RagMonkError::usage(format!("no such source: {id}")))?],
        None => cp.list_sources(false).map_err(db)?,
    };
    let layout = V2Layout::new(home);
    let mut out = Vec::new();
    for source in sources {
        let Some(build) = cp.state(&source.id).map_err(db)?.active_build_id else {
            continue;
        };
        let (store, _) = ProjectStore::open(
            &layout,
            &project_id_for_canonical(&source.path),
            &source.id,
            cfg.runtime.sqlite_cache_size_mb,
        )
        .map_err(db)?;
        out.push(Opened {
            source,
            store,
            build,
        });
    }
    Ok(out)
}

pub fn corpora(opened: &[Opened]) -> Vec<Corpus<'_>> {
    opened
        .iter()
        .map(|o| Corpus {
            store: &o.store,
            build_id: &o.build,
        })
        .collect()
}

fn search_err(e: ragmonk_retrieval::SearchError) -> RagMonkError {
    RagMonkError::new(ErrorKind::Database, e.to_string())
}

// ------------------------------------------------------------ paths ---
//
// Retrieval reports paths relative to the source root. The reference
// prints absolute paths, so the CLI joins them with the source's path.

fn root_of<'a>(opened: &'a [Opened], source_id: &str) -> Option<&'a str> {
    opened
        .iter()
        .find(|o| o.source.id == source_id)
        .map(|o| o.source.path.as_str())
}

fn join(root: &str, rel: &str) -> String {
    if rel.is_empty() || Path::new(rel).is_absolute() {
        return rel.to_owned();
    }
    Path::new(root).join(rel).to_string_lossy().into_owned()
}

/// `rel:line` (or a bare path) under `root`.
fn join_location(root: &str, loc: &str) -> String {
    match loc.rsplit_once(':') {
        Some((p, line)) if line.chars().all(|c| c.is_ascii_digit()) && !line.is_empty() => {
            format!("{}:{line}", join(root, p))
        }
        _ => join(root, loc),
    }
}

fn absolutize_results(results: &mut [SearchResult], opened: &[Opened]) {
    for r in results {
        let Some(root) = root_of(opened, &r.source_id) else {
            continue;
        };
        if r.kind == "path" && r.title == r.path {
            r.title = join(root, &r.title);
        }
        r.path = join(root, &r.path);
    }
}

fn absolutize_hits(hits: &mut [hybrid::SourcedHit], opened: &[Opened]) {
    for h in hits {
        if let Some(root) = root_of(opened, &h.source_id) {
            h.hit.path = join(root, &h.hit.path);
        }
    }
}

/// Rewrites every `path` / `paths` / `source_location` string in an
/// `explore` or `impact` payload. An object with a `source_id` uses that
/// source. Otherwise the first source where the file exists is used,
/// falling back to the first source.
fn absolutize_json(v: &mut Value, opened: &[Opened], inherited: Option<&str>) {
    let pick = |rel: &str, sid: Option<&str>| -> Option<String> {
        if let Some(root) = sid.and_then(|s| root_of(opened, s)) {
            return Some(root.to_owned());
        }
        let file = rel.rsplit_once(':').map_or(rel, |(p, _)| p);
        opened
            .iter()
            .find(|o| Path::new(&o.source.path).join(file).exists())
            .or_else(|| opened.first())
            .map(|o| o.source.path.clone())
    };
    match v {
        Value::Object(map) => {
            let own = map
                .get("source_id")
                .and_then(Value::as_str)
                .map(str::to_owned);
            let sid = own.as_deref().or(inherited);
            for (k, child) in map.iter_mut() {
                match (k.as_str(), &mut *child) {
                    ("path", Value::String(p)) => {
                        if let Some(root) = pick(p, sid) {
                            *p = join(&root, p);
                        }
                    }
                    ("source_location", Value::String(p)) => {
                        if let Some(root) = pick(p, sid) {
                            *p = join_location(&root, p);
                        }
                    }
                    ("paths", Value::Array(items)) => {
                        for it in items.iter_mut() {
                            if let Value::String(p) = it {
                                if let Some(root) = pick(p, sid) {
                                    *p = join(&root, p);
                                }
                            }
                        }
                    }
                    _ => absolutize_json(child, opened, sid),
                }
            }
        }
        Value::Array(items) => {
            for it in items {
                absolutize_json(it, opened, inherited);
            }
        }
        _ => {}
    }
}

// ------------------------------------------------------ code graph ---

fn match_json(m: &SourceMatch, opened: &[Opened]) -> Value {
    let e = &m.entity;
    json!({
        "entity_id": e.id,
        "kind": e.kind,
        "name": e.name,
        "qualified_name": e.qualified_name,
        "language": e.language,
        "file_id": e.file_id,
        "start_line": e.start_line,
        "end_line": e.end_line,
        "signature": e.signature,
        "source_id": m.source_id,
        "source_path": opened.get(m.corpus).map(|o| o.source.path.clone()),
    })
}

fn edge_json(e: &TraversalEdge, opened: &[Opened]) -> Value {
    let r = &e.relationship;
    let root = opened.get(e.corpus).map(|o| o.source.path.as_str());
    json!({
        "depth": e.depth,
        "relationship_type": r.relationship_type,
        "source_entity_id": r.source_entity_id,
        "target_entity_id": r.target_entity_id,
        "target_symbol": r.target_symbol,
        "confidence": r.confidence,
        "resolver": r.resolver,
        "source_location": match (root, &r.source_location) {
            (Some(root), Some(l)) => Some(join_location(root, l)),
            (_, l) => l.clone(),
        },
        "evidence": r.evidence,
    })
}

fn no_matches(name: &str) {
    println!("No entities found matching '{name}'.");
}

fn print_matches(matches: &[SourceMatch]) {
    let rows: Vec<Vec<String>> = matches
        .iter()
        .map(|m| {
            vec![
                m.entity.kind.clone(),
                m.entity.qualified_name.clone(),
                m.entity.language.clone(),
                format!("{}:{}", m.entity.file_id, m.entity.start_line),
                m.source_id.clone(),
            ]
        })
        .collect();
    print_table(
        &["Kind", "Qualified name", "Language", "Location", "Source"],
        &rows,
    );
}

fn print_edges(edges: &[TraversalEdge]) {
    let rows: Vec<Vec<String>> = edges
        .iter()
        .map(|e| {
            let r = &e.relationship;
            vec![
                e.depth.to_string(),
                r.relationship_type.clone(),
                r.confidence.clone(),
                r.target_entity_id
                    .clone()
                    .unwrap_or_else(|| format!("~{}", r.target_symbol.clone().unwrap_or_default())),
                r.source_location.clone().unwrap_or_else(|| "-".into()),
            ]
        })
        .collect();
    print_table(
        &["Depth", "Type", "Confidence", "Target", "Location"],
        &rows,
    );
}

#[derive(clap::Args)]
pub struct GraphArgs {
    /// Symbol name or fully qualified name.
    name: String,
    #[arg(long = "max-depth", default_value_t = graph::DEFAULT_MAX_DEPTH,
          value_parser = clap::builder::RangedU64ValueParser::<usize>::new().range(1..=10))]
    max_depth: usize,
    #[arg(long, default_value_t = graph::DEFAULT_LIMIT,
          value_parser = clap::builder::RangedU64ValueParser::<usize>::new().range(1..=1000))]
    limit: usize,
    #[arg(long = "json")]
    json: bool,
}

pub fn symbol(name: &str, json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let opened = open_sources(&home, None)?;
    let matches = graph::find_symbol_matches(&corpora(&opened), name).map_err(search_err)?;
    if json_output {
        let m: Vec<Value> = matches.iter().map(|m| match_json(m, &opened)).collect();
        return print_json(&json!({"query": name, "matches": m}));
    }
    if matches.is_empty() {
        no_matches(name);
        return Ok(());
    }
    print_matches(&matches);
    Ok(())
}

/// `callers` (incoming CALLS) and `callees` (outgoing CALLS).
pub fn calls(a: &GraphArgs, direction: Direction) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let opened = open_sources(&home, None)?;
    let (matches, edges) = graph::traverse_symbol(
        &corpora(&opened),
        &a.name,
        direction,
        &graph::CALL_TYPES,
        a.max_depth,
        a.limit,
    )
    .map_err(search_err)?;
    graph_output(
        a,
        &opened,
        &matches,
        &edges,
        matches.is_empty() && edges.is_empty(),
    )
}

pub fn references(a: &GraphArgs) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let opened = open_sources(&home, None)?;
    let (matches, edges) =
        graph::references(&corpora(&opened), &a.name, a.max_depth, a.limit).map_err(search_err)?;
    graph_output(a, &opened, &matches, &edges, matches.is_empty())
}

fn graph_output(
    a: &GraphArgs,
    opened: &[Opened],
    matches: &[SourceMatch],
    edges: &[TraversalEdge],
    nothing: bool,
) -> Result<(), RagMonkError> {
    if a.json {
        return print_json(&json!({
            "query": a.name,
            "matches": matches.iter().map(|m| match_json(m, opened)).collect::<Vec<_>>(),
            "edges": edges.iter().map(|e| edge_json(e, opened)).collect::<Vec<_>>(),
        }));
    }
    if nothing {
        no_matches(&a.name);
        return Ok(());
    }
    print_edges(edges);
    Ok(())
}

fn format_location(d: &Value) -> String {
    let path = d["path"].as_str().unwrap_or_default();
    let loc = &d["location"];
    if let Some(s) = loc["section"].as_str().filter(|s| !s.is_empty()) {
        return format!("{path} §{s}");
    }
    if !loc["page"].is_null() {
        return format!("{path} p{}", loc["page"]);
    }
    path.to_owned()
}

pub fn impact(a: &GraphArgs) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let opened = open_sources(&home, None)?;
    let mut payload =
        explore::impact(&corpora(&opened), &a.name, a.max_depth, a.limit).map_err(search_err)?;
    absolutize_json(&mut payload, &opened, None);
    if a.json {
        return print_json(&payload);
    }
    if payload["found"] != true {
        no_matches(&a.name);
        return Ok(());
    }
    let list = |k: &str| -> String {
        let v: Vec<&str> = payload[k]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect();
        if v.is_empty() {
            "-".into()
        } else {
            v.join(", ")
        }
    };
    println!("{}", a.name);
    for d in payload["defined"].as_array().into_iter().flatten() {
        println!(
            "Defined: {}:{}",
            d["path"].as_str().unwrap_or_default(),
            d["start_line"]
        );
    }
    println!("Callers: {}", list("callers"));
    println!("Callees: {}", list("callees"));
    println!("Tests: {}", list("tests"));
    let docs: Vec<String> = payload["documentation"]
        .as_array()
        .into_iter()
        .flatten()
        .map(format_location)
        .collect();
    println!(
        "Documentation: {}",
        if docs.is_empty() {
            "-".into()
        } else {
            docs.join(", ")
        }
    );
    let c = &payload["confidence"];
    println!(
        "Confidence: Code references: {} / Document links: {}",
        c["code_references"].as_str().unwrap_or("none"),
        c["document_links"].as_str().unwrap_or("none")
    );
    println!(
        "Blast radius: {}",
        payload["blast_radius"].as_str().unwrap_or_default()
    );
    Ok(())
}

// --------------------------------------------------------- semantic ---

fn lazy_embedder(home: &Home, cfg: &ragmonk_config::RagMonkConfig) -> ragmonk_ml::LazyEmbedder {
    ragmonk_ml::LazyEmbedder::new(
        ragmonk_ml::embedder::models_root(Some(&home.root().join("models"))),
        ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL,
        cfg.indexing.embedding_batch_size,
    )
}

fn semantic_json(s: &hybrid::SemanticOutcome) -> Value {
    json!({
        "available": s.available,
        "reason": s.reason,
        "results": s.hits.iter().map(explore::semantic_hit_json).collect::<Vec<_>>(),
    })
}

// ---------------------------------------------------------- explore ---

pub fn explore(query: &str, json_output: bool) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let cfg = load(&home)?;
    let opened = open_sources(&home, None)?;
    let corp = corpora(&opened);
    let plan = explore::plan(query, cfg.search.semantic);
    let semantic = if plan.strategies.contains(&explore::Strategy::Semantic) {
        let lazy = lazy_embedder(&home, &cfg);
        let embedder = lazy.get().ok().map(|a| a.as_ref());
        Some(
            hybrid::semantic_search(
                &corp,
                embedder,
                query,
                lexical::DEFAULT_LIMIT,
                Some(cfg.search.semantic_top_k.max(1) as usize),
            )
            .map_err(search_err)?,
        )
    } else {
        None
    };
    let opts = explore::ExploreOptions {
        budget: explore::Budget {
            max_chars: cfg.context.max_chars.max(0) as usize,
            max_files: cfg.context.max_files.max(0) as usize,
            max_graph_nodes: cfg.context.max_graph_nodes.max(0) as usize,
        },
        snippet_tokens: cfg.search.output.snippet_max_tokens,
    };
    let mut r = explore::explore(&corp, &plan, semantic.as_ref(), opts).map_err(search_err)?;
    absolutize_json(&mut r, &opened, None);
    if json_output {
        return print_json(&r);
    }
    let strs = |v: &Value| -> Vec<String> {
        v.as_array()
            .into_iter()
            .flatten()
            .map(|x| x.as_str().map_or_else(|| x.to_string(), str::to_owned))
            .collect()
    };
    let line = |label: &str, items: Vec<String>| {
        println!(
            "{label}: {}",
            if items.is_empty() {
                "-".into()
            } else {
                items.join(", ")
            }
        );
    };
    let arr = |k: &str| r[k].as_array().cloned().unwrap_or_default();
    println!("{}", r["summary"].as_str().unwrap_or_default());
    println!(
        "Intent: {}  Strategies: {}",
        r["intent"].as_str().unwrap_or_default(),
        strs(&r["strategies"]).join(", ")
    );
    line(
        "Relevant symbols",
        arr("symbols")
            .iter()
            .map(|s| s["qualified_name"].as_str().unwrap_or_default().to_owned())
            .collect(),
    );
    line("Relevant paths", strs(&r["paths"]));
    line(
        "Call flows",
        arr("call_flows")
            .iter()
            .map(|p| {
                format!(
                    "{} -[{}]-> {}",
                    p["source"].as_str().unwrap_or_default(),
                    p["relationship"].as_str().unwrap_or_default(),
                    p["target"].as_str().unwrap_or_default()
                )
            })
            .collect(),
    );
    line(
        "Dependencies",
        arr("dependencies")
            .iter()
            .map(|d| {
                format!(
                    "{} ({})",
                    d["qualified_name"].as_str().unwrap_or_default(),
                    d["relationship"].as_str().unwrap_or_default()
                )
            })
            .collect(),
    );
    line(
        "Documents",
        arr("documents")
            .iter()
            .map(|d| {
                d["title"]
                    .as_str()
                    .filter(|t| !t.is_empty())
                    .or(d["path"].as_str())
                    .unwrap_or_default()
                    .to_owned()
            })
            .collect(),
    );
    line("Tests", strs(&r["tests"]));
    line("Requirements", strs(&r["requirements"]));
    line("Incidents", strs(&r["incidents"]));
    let sem = arr("semantic_results");
    if !sem.is_empty() {
        line(
            "Semantic matches",
            sem.iter()
                .map(|h| {
                    format!(
                        "{} ({:.3})",
                        h["title"].as_str().unwrap_or_default(),
                        h["score"].as_f64().unwrap_or(0.0)
                    )
                })
                .collect(),
        );
    } else if r["semantic_available"] == false {
        println!(
            "Semantic search unavailable: {}",
            r["semantic_reason"].as_str().unwrap_or_default()
        );
    }
    println!(
        "Evidence: {} item(s){}",
        arr("evidence").len(),
        if r["evidence_truncated"] == true {
            " (truncated)"
        } else {
            ""
        }
    );
    Ok(())
}

// ----------------------------------------------------------- search ---

#[derive(clap::Args)]
pub struct SearchArgs {
    /// Identifier, phrase, or path fragment to search for.
    query: String,
    #[arg(long, default_value_t = lexical::DEFAULT_LIMIT,
          value_parser = clap::builder::RangedU64ValueParser::<usize>::new().range(1..=200))]
    limit: usize,
    #[arg(long = "json")]
    json: bool,
    /// Show document hits as match-centered snippet blocks (the default;
    /// see search.output.fallback in config.yaml).
    #[arg(long)]
    snippets: bool,
    /// Show every hit as a plain title/path/tier table.
    #[arg(long)]
    table: bool,
    /// Show per-stage timing diagnostics.
    #[arg(long)]
    explain: bool,
    /// Also show one merged, reranked view of lexical and semantic results.
    #[arg(long)]
    hybrid: bool,
}

fn hits_table(results: &[&SearchResult]) {
    let rows: Vec<Vec<String>> = results
        .iter()
        .map(|r| {
            vec![
                r.kind.to_owned(),
                r.tier.label().to_owned(),
                r.title.clone(),
                r.path.clone(),
                r.source_id.clone(),
            ]
        })
        .collect();
    print_table(&["Kind", "Tier", "Title", "Path", "Source"], &rows);
}

fn format_label(path: &str) -> String {
    let ext = Path::new(path)
        .extension()
        .map(|e| e.to_string_lossy().to_uppercase())
        .unwrap_or_default();
    if ext.is_empty() {
        "FILE".into()
    } else {
        ext
    }
}

fn print_expanded(c: &Value) {
    let prev = c["previous"].as_array().cloned().unwrap_or_default();
    let next = c["next"].as_array().cloned().unwrap_or_default();
    if c["parent_heading"].is_null() && prev.is_empty() && next.is_empty() {
        return;
    }
    println!("Expanded context:");
    if let Some(t) = c["parent_heading"]["text"].as_str() {
        println!("[heading] {t}");
    }
    for p in &prev {
        println!("[previous] {}", p["text"].as_str().unwrap_or_default());
    }
    for p in &next {
        println!("[next] {}", p["text"].as_str().unwrap_or_default());
    }
    println!();
}

fn print_document_snippet(r: &SearchResult, fallback: &[String], expanded: Option<&Value>) {
    let snippet = r.snippet.as_deref().filter(|s| !s.is_empty());
    let Some(snippet) = snippet else {
        let next = fallback
            .iter()
            .find(|m| matches!(m.as_str(), "json" | "files"))
            .map_or("files", String::as_str);
        if next == "json" {
            println!(
                "{}",
                serde_json::to_string_pretty(&explore::search_result_json(r)).unwrap_or_default()
            );
        } else {
            println!("{}", r.path);
        }
        return;
    };
    println!("{}: {}", format_label(&r.path), r.path);
    let loc = r.location.as_ref();
    if let Some(a) = loc.and_then(|l| l.attachment.as_ref()) {
        let a = json!(a);
        println!(
            "Attachment: {} -> {}",
            r.path,
            a["name"].as_str().unwrap_or_default()
        );
        if let Some(t) = a["parent_title"].as_str().filter(|t| !t.is_empty()) {
            println!("Email: {t}");
        }
    }
    if let Some(l) = loc {
        if let Some(start) = l.page_start {
            match l.page_end {
                Some(end) if end != start => println!("Page: {start}-{end}"),
                _ => println!("Page: {start}"),
            }
        } else if !l.heading_path.is_empty() {
            println!("Section: {}", l.heading_path.join(" > "));
        } else if let Some(s) = l.section.as_deref().filter(|s| !s.is_empty()) {
            println!("Section: {s}");
        }
    }
    println!("Match:");
    println!("{snippet}");
    if let Some(c) = expanded {
        print_expanded(c);
    }
    println!();
}

pub fn search(a: &SearchArgs) -> Result<(), RagMonkError> {
    let home = prepared_home()?;
    let cfg = load(&home)?;
    let sc = &cfg.search;
    let opened = open_sources(&home, None)?;
    let corp = corpora(&opened);
    let (mut results, stage_timings) =
        lexical::search_with_timings(&corp, &a.query, a.limit, sc.output.snippet_max_tokens)
            .map_err(search_err)?;
    absolutize_results(&mut results, &opened);
    let mut timings: Vec<Value> = stage_timings
        .iter()
        .map(|t| json!({"stage": t.stage, "hits": t.hits, "duration_ms": t.duration_ms}))
        .collect();
    let confidence = classify::estimate_confidence(&results);
    let run_semantic = sc.semantic && !(sc.lazy_semantic && confidence == "high");
    let lazy = lazy_embedder(&home, &cfg);
    let semantic = if run_semantic {
        let started = Instant::now();
        let embedder = lazy.get().ok().map(|a| a.as_ref());
        let mut s = hybrid::semantic_search(
            &corp,
            embedder,
            &a.query,
            a.limit,
            Some(sc.semantic_top_k.max(1) as usize),
        )
        .map_err(search_err)?;
        absolutize_hits(&mut s.hits, &opened);
        timings.push(json!({
            "stage": "semantic",
            "hits": s.hits.len(),
            "duration_ms": started.elapsed().as_secs_f64() * 1000.0,
        }));
        Some(s)
    } else {
        None
    };
    let ranked = if a.hybrid {
        let sem_hits = semantic
            .as_ref()
            .map(|s| s.hits.clone())
            .unwrap_or_default();
        let candidates = hybrid::merge(&results, &sem_hits);
        if sc.reranker.enabled {
            let top_n = sc.reranker.top_n.max(1) as usize;
            let pooled = hybrid::rerank(candidates, a.limit.max(top_n));
            let started = Instant::now();
            let root = ragmonk_ml::embedder::models_root(Some(&home.root().join("models")));
            let spec = ragmonk_ml::manifest::DEFAULT_RERANKER_MODEL;
            let encoder =
                ragmonk_ml::reranker::CrossEncoder::load(&root.join(spec.slug), spec).ok();
            let mut hits = hybrid::neural_rerank(&a.query, pooled, top_n, encoder.as_ref());
            timings.push(json!({
                "stage": "neural_rerank",
                "hits": hits.len().min(top_n),
                "duration_ms": started.elapsed().as_secs_f64() * 1000.0,
            }));
            hits.truncate(a.limit);
            Some(hits)
        } else {
            Some(hybrid::rerank(candidates, a.limit))
        }
    } else {
        None
    };
    let mode = if a.json {
        "json".to_owned()
    } else if a.snippets {
        "snippets".into()
    } else if a.table {
        "table".into()
    } else {
        sc.output.fallback[0].clone()
    };

    // Context for document hits, after ranking and the limit.
    let ctx_opts = context::ContextOptions::from_config(&sc.context);
    let mut expanded: HashMap<String, Value> = HashMap::new();
    if matches!(mode.as_str(), "json" | "snippets") && !ctx_opts.disabled() {
        for r in results.iter().filter(|r| r.kind == "document") {
            if let Some(o) = opened.iter().find(|o| o.source.id == r.source_id) {
                if let Some(c) = context::expand_chunk_context(&o.store, &o.build, &r.id, &ctx_opts)
                    .map_err(search_err)?
                {
                    expanded.insert(r.id.clone(), c);
                }
            }
        }
    }
    let total_ms: f64 = timings
        .iter()
        .map(|t| t["duration_ms"].as_f64().unwrap_or(0.0))
        .sum();
    let tok = || {
        use ragmonk_documents::tokenizer as t;
        json!({
            "model_id": t::EMBEDDING_MODEL_ID,
            "revision": t::TOKENIZER_REVISION,
            "fingerprint": t::tokenizer_fingerprint(),
            "max_sequence_tokens": t::MAX_SEQUENCE_TOKENS,
        })
    };

    if mode == "json" {
        let results_json: Vec<Value> = results
            .iter()
            .map(|r| {
                let mut v = explore::search_result_json(r);
                if let Some(c) = expanded.get(&r.id) {
                    v["context"] = c.clone();
                }
                v
            })
            .collect();
        let mut payload = json!({"query": a.query, "results": results_json});
        if let Some(s) = &semantic {
            payload["semantic"] = semantic_json(s);
        }
        if let Some(h) = &ranked {
            payload["hybrid"] = json!(h.iter().map(hybrid::RankedHit::to_json).collect::<Vec<_>>());
        }
        if a.explain {
            let round3 = |v: f64| (v * 1000.0).round() / 1000.0;
            payload["explain"] = json!({
                "query_kind": classify::classify_query(&a.query),
                "lexical_confidence": confidence,
                "semantic_skipped": sc.semantic && !run_semantic,
                "stages": timings.iter().map(|t| {
                    let mut t = t.clone();
                    t["duration_ms"] = json!(round3(t["duration_ms"].as_f64().unwrap_or(0.0)));
                    t
                }).collect::<Vec<_>>(),
                "total_ms": round3(total_ms),
                "tokenizer": tok(),
            });
        }
        return print_json(&payload);
    }

    if results.is_empty() {
        println!("No results for '{}'.", a.query);
    } else if mode == "table" {
        hits_table(&results.iter().collect::<Vec<_>>());
    } else {
        let others: Vec<&SearchResult> = results.iter().filter(|r| r.kind != "document").collect();
        let docs: Vec<&SearchResult> = results.iter().filter(|r| r.kind == "document").collect();
        if !others.is_empty() {
            hits_table(&others);
        }
        if mode == "files" {
            let mut seen = std::collections::HashSet::new();
            for r in docs {
                if seen.insert(r.path.clone()) {
                    println!("{}", r.path);
                }
            }
        } else {
            let fallback: Vec<String> = sc
                .output
                .fallback
                .iter()
                .filter(|m| m.as_str() != "snippets")
                .cloned()
                .collect();
            for r in docs {
                print_document_snippet(r, &fallback, expanded.get(&r.id));
            }
        }
    }

    if let Some(s) = &semantic {
        if !s.hits.is_empty() {
            println!("Semantic matches");
            let rows: Vec<Vec<String>> = s
                .hits
                .iter()
                .map(|h| {
                    vec![
                        h.hit.kind.clone(),
                        format!("{:.3}", h.hit.score),
                        h.hit.title.clone(),
                        h.hit.path.clone(),
                        h.source_id.clone(),
                    ]
                })
                .collect();
            print_table(&["Kind", "Score", "Title", "Path", "Source"], &rows);
        } else if !s.available {
            println!("Semantic search unavailable: {}", s.reason);
        }
    } else if sc.semantic {
        println!("Semantic search skipped: lexical confidence is {confidence}.");
    }
    if let Some(hits) = &ranked {
        println!("Hybrid ranked results");
        let rows: Vec<Vec<String>> = hits
            .iter()
            .map(|h| {
                let c = &h.candidate;
                vec![
                    c.kind.clone(),
                    h.tier_label.to_owned(),
                    c.title.clone(),
                    c.path.clone(),
                    c.semantic_score
                        .map_or_else(|| "-".into(), |s| format!("{s:.3}")),
                ]
            })
            .collect();
        print_table(&["Kind", "Tier", "Title", "Path", "Semantic"], &rows);
    }
    if a.explain {
        println!(
            "Query kind: {}  Lexical confidence: {confidence}",
            classify::classify_query(&a.query)
        );
        let rows: Vec<Vec<String>> = timings
            .iter()
            .map(|t| {
                vec![
                    t["stage"].as_str().unwrap_or_default().to_owned(),
                    t["hits"].to_string(),
                    format!("{:.3}", t["duration_ms"].as_f64().unwrap_or(0.0)),
                ]
            })
            .collect();
        print_table(&["Stage", "Hits", "Duration (ms)"], &rows);
        println!("Total: {total_ms:.3} ms");
        let t = tok();
        println!(
            "Tokenizer: {} (revision {}, max {} tokens)",
            t["model_id"].as_str().unwrap_or_default(),
            &t["revision"].as_str().unwrap_or_default()[..12],
            t["max_sequence_tokens"]
        );
    }
    Ok(())
}

// ------------------------------------------------------------- link ---

#[derive(Subcommand)]
pub enum LinkCommand {
    /// Link a code entity to a document (resolver "user", confidence exact).
    Add {
        /// Entity id, name, or qualified name.
        entity: String,
        /// Document id, file path, or filename.
        document: String,
        /// Specific section (chunk) id within the document.
        #[arg(long)]
        section: Option<String>,
        /// Restrict lookup to this source id.
        #[arg(long = "source")]
        source: Option<String>,
    },
    /// Remove a manual link by id, as shown by 'ragmonk link list'.
    Remove {
        link_id: String,
        #[arg(long = "source")]
        source: Option<String>,
    },
    /// List links.
    List {
        /// Only links for this entity id/name.
        #[arg(long)]
        entity: Option<String>,
        /// Only links for this document id.
        #[arg(long = "document")]
        document: Option<String>,
        #[arg(long = "source")]
        source: Option<String>,
        #[arg(long = "json")]
        json: bool,
    },
}

pub fn link(cmd: LinkCommand) -> Result<(), RagMonkError> {
    use ragmonk_knowledge::manual;
    let home = prepared_home()?;
    match cmd {
        LinkCommand::Add {
            entity,
            document,
            section,
            source,
        } => {
            let mut opened = open_sources(&home, source.as_deref())?;
            let mut hits = Vec::new();
            for (i, o) in opened.iter().enumerate() {
                let e = manual::resolve_entity(&o.store, &o.build, &entity);
                let d = manual::resolve_document(&o.store, &o.build, &document);
                if let (Ok(e), Ok(d)) = (e, d) {
                    hits.push((i, e, d));
                }
            }
            let (i, e, (_, _, document_id)) = match hits.len() {
                0 => {
                    return Err(RagMonkError::usage(format!(
                        "no unambiguous match for entity '{entity}' and document '{document}'"
                    )))
                }
                1 => hits.remove(0),
                _ => {
                    let ids: Vec<&str> = hits
                        .iter()
                        .map(|(i, _, _)| opened[*i].source.id.as_str())
                        .collect();
                    return Err(RagMonkError::usage(format!(
                        "ambiguous match across sources ({}); pass --source to disambiguate",
                        ids.join(", ")
                    )));
                }
            };
            let o = &mut opened[i];
            let ordinal = match &section {
                None => None,
                Some(sid) => match o.store.chunk(&o.build, sid).map_err(db)? {
                    Some(c) if c.document_id == document_id => Some(c.ordinal),
                    _ => {
                        return Err(RagMonkError::usage(format!(
                            "no such section '{sid}' in document {document_id}"
                        )))
                    }
                },
            };
            let now = ragmonk_indexing::progress::now_iso();
            let added = manual::add(
                &mut o.store,
                &o.build,
                &e.id,
                &document_id,
                ordinal,
                None,
                &now,
            )
            .map_err(|err| RagMonkError::usage(err.to_string()))?;
            match added {
                None => println!("That link already exists."),
                Some(l) => println!("Linked {} -> {document_id} ({})", e.qualified_name, l.id),
            }
        }
        LinkCommand::Remove { link_id, source } => {
            for mut o in open_sources(&home, source.as_deref())? {
                if manual::remove(&mut o.store, &o.build, &link_id)
                    .map_err(|err| RagMonkError::usage(err.to_string()))?
                {
                    println!("Removed {link_id}");
                    return Ok(());
                }
            }
            return Err(RagMonkError::usage(format!("no such link: {link_id}")));
        }
        LinkCommand::List {
            entity,
            document,
            source,
            json: json_output,
        } => {
            let mut rows = Vec::new();
            for o in open_sources(&home, source.as_deref())? {
                rows.extend(link_rows(&o, entity.as_deref(), document.as_deref())?);
            }
            if json_output {
                return print_json(&json!({ "links": rows }));
            }
            if rows.is_empty() {
                println!("No links found.");
                return Ok(());
            }
            let s = |v: &Value| v.as_str().unwrap_or_default().to_owned();
            let table: Vec<Vec<String>> = rows
                .iter()
                .map(|r| {
                    vec![
                        s(&r["link_id"]),
                        s(&r["link_type"]),
                        r["entity"]
                            .as_str()
                            .map_or_else(|| s(&r["entity_id"]), str::to_owned),
                        r["document_path"]
                            .as_str()
                            .map_or_else(|| s(&r["document_id"]), str::to_owned),
                        s(&r["resolver"]),
                        s(&r["confidence"]),
                    ]
                })
                .collect();
            print_table(
                &["ID", "Type", "Entity", "Document", "Resolver", "Confidence"],
                &table,
            );
        }
    }
    Ok(())
}

/// `link list` rows for one source. A user link shows its manual link id,
/// which is what `link remove` takes.
fn link_rows(
    o: &Opened,
    entity: Option<&str>,
    document: Option<&str>,
) -> Result<Vec<Value>, RagMonkError> {
    let store = &o.store;
    let mut links = store.links(&o.build).map_err(db)?;
    if let Some(r) = entity {
        let ids: std::collections::HashSet<String> = match store.entity(&o.build, r).map_err(db)? {
            Some(e) => [e.id].into(),
            None => store
                .entities_named(&o.build, r)
                .map_err(db)?
                .into_iter()
                .map(|e| e.id)
                .collect(),
        };
        links.retain(|l| ids.contains(&l.entity_id));
    } else if let Some(d) = document {
        links.retain(|l| l.document_id == d);
    }
    let manual_ids = ragmonk_knowledge::manual::manual_ids(store, &o.build).map_err(db)?;
    let files: HashMap<String, String> = store
        .files(&o.build)
        .map_err(db)?
        .into_iter()
        .map(|f| (f.id, f.rel_path))
        .collect();
    let doc_files: HashMap<String, String> = store
        .documents(&o.build)
        .map_err(db)?
        .into_iter()
        .map(|d| (d.id, d.file_id))
        .collect();
    let mut out = Vec::new();
    for l in links {
        let entity_qn = store
            .entity(&o.build, &l.entity_id)
            .map_err(db)?
            .map(|e| e.qualified_name);
        let document_path = doc_files
            .get(&l.document_id)
            .and_then(|f| files.get(f))
            .map(|rel| {
                Path::new(&o.source.path)
                    .join(rel)
                    .to_string_lossy()
                    .into_owned()
            });
        out.push(json!({
            "link_id": manual_ids.get(&l.id).cloned().unwrap_or_else(|| l.id.clone()),
            "link_type": l.link_type,
            "source_id": o.source.id,
            "entity_id": l.entity_id,
            "entity": entity_qn,
            "document_id": l.document_id,
            "document_path": document_path,
            "section_id": l.chunk_id,
            "resolver": l.resolver,
            "confidence": l.confidence,
            "evidence": l.evidence,
        }));
    }
    Ok(out)
}
