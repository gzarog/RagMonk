//! `ragmonk serve --mcp` (RUST-13): the stdio MCP server.
//!
//! A hand-rolled JSON-RPC 2.0 loop over newline-delimited stdin/stdout
//! that answers like the reference FastMCP server: the same `initialize`
//! capabilities, the same eight read-only `ragmonk_*` tools (catalog in
//! `mcp_tools.json`, captured from the reference), the same argument
//! validation messages and the same `{schema_version, ok, error, ...}`
//! tool results. `ragmonk_ask` is not served (ADR 0025).
//!
//! Each tool call loads the config and opens the indexed sources afresh,
//! like a CLI invocation, on a worker thread bounded by
//! `mcp.request_timeout_seconds`. Errors never cross the boundary as
//! protocol failures: they become `ok: false` results typed by the
//! reference's exception class name, `timeout` or `internal_error`.

use std::io::{BufRead, Write};
use std::sync::mpsc;
use std::sync::OnceLock;
use std::time::Duration;

use ragmonk_core::errors::{ErrorKind, RagMonkError};
use ragmonk_core::paths::Home;
use ragmonk_retrieval::graph::{self, Direction};
use ragmonk_retrieval::lexical;
use serde_json::{json, Map, Value};

use crate::query_cmd::{self, open_sources};
use crate::{load, prepared_home, status_cmd, workflow};

/// Protocol versions the server accepts; anything else gets the latest.
const SUPPORTED_PROTOCOL_VERSIONS: &[&str] =
    &["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];
const LATEST_PROTOCOL_VERSION: &str = "2025-11-25";
/// The reference reports its MCP SDK version as `serverInfo.version`.
const SERVER_VERSION: &str = "1.30.0";
const SCHEMA_VERSION: &str = "1";
const PYDANTIC_DOCS: &str = "https://errors.pydantic.dev/2.13/v";

pub const INSTRUCTIONS: &str =
    "RagMonk exposes code and document knowledge already indexed locally \
on this machine (see `ragmonk index`). Start with ragmonk_explore for a natural-language or \
identifier query -- it is the primary tool and runs RagMonk's deterministic query planner. The \
other read-only tools (ragmonk_search/symbol/callers/callees/impact/documents/status) are \
narrower, single-purpose lookups; all 8 of them return a bounded, versioned JSON result \
(schema_version/ok/error) and never call an LLM or the network -- results are \
retrieved/structured data for the calling agent to reason over.";

/// The tool catalog `tools/list` returns.
pub fn tools() -> &'static [Value] {
    static TOOLS: OnceLock<Vec<Value>> = OnceLock::new();
    TOOLS.get_or_init(|| {
        serde_json::from_str(include_str!("mcp_tools.json")).expect("valid mcp_tools.json")
    })
}

fn tool(name: &str) -> Option<&'static Value> {
    tools().iter().find(|t| t["name"] == name)
}

/// `ragmonk serve [--mcp]`.
pub fn serve(mcp: bool) -> Result<(), RagMonkError> {
    if !mcp {
        return Err(RagMonkError::usage(
            "ragmonk serve currently requires --mcp (the REST API lands in a later phase).",
        ));
    }
    let home = prepared_home()?;
    if !load(&home)?.mcp.enabled {
        return Err(RagMonkError::new(
            ErrorKind::Config,
            "the MCP server is disabled (mcp.enabled=false in config); run `ragmonk config set \
             mcp.enabled true` to enable it.",
        ));
    }
    // stdout is the transport: diagnostics go to stderr only.
    eprintln!("RagMonk MCP server starting on stdio...");
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout();
    for line in stdin.lock().lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        if let Some(reply) = handle_line(&line) {
            let text = serde_json::to_string(&reply).unwrap_or_default();
            if writeln!(stdout, "{text}")
                .and_then(|()| stdout.flush())
                .is_err()
            {
                break;
            }
        }
    }
    Ok(())
}

/// One JSON-RPC message in, at most one message out.
pub fn handle_line(line: &str) -> Option<Value> {
    let Ok(msg) = serde_json::from_str::<Value>(line) else {
        // The reference logs the parse failure to the client.
        return Some(json!({
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {
                "level": "error",
                "logger": "mcp.server.exception_handler",
                "data": "Internal Server Error",
            },
        }));
    };
    let method = msg.get("method")?.as_str()?;
    // Notifications (no id) and responses get no reply.
    let id = msg.get("id").filter(|id| !id.is_null())?.clone();
    let params = msg.get("params").cloned().unwrap_or(Value::Null);
    let outcome = match method {
        "initialize" => Ok(initialize(&params)),
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({"tools": tools()})),
        "prompts/list" => Ok(json!({"prompts": []})),
        "resources/list" => Ok(json!({"resources": []})),
        "resources/templates/list" => Ok(json!({"resourceTemplates": []})),
        "tools/call" => Ok(call_tool(&params)),
        _ => Err(json!({"code": -32602, "message": "Invalid request parameters", "data": ""})),
    };
    Some(match outcome {
        Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
        Err(error) => json!({"jsonrpc": "2.0", "id": id, "error": error}),
    })
}

fn initialize(params: &Value) -> Value {
    let requested = params["protocolVersion"].as_str().unwrap_or_default();
    let version = if SUPPORTED_PROTOCOL_VERSIONS.contains(&requested) {
        requested
    } else {
        LATEST_PROTOCOL_VERSION
    };
    json!({
        "protocolVersion": version,
        "capabilities": {
            "experimental": {},
            "prompts": {"listChanged": false},
            "resources": {"subscribe": false, "listChanged": false},
            "tools": {"listChanged": false},
        },
        "serverInfo": {
            "name": format!("ragmonk v{}", ragmonk_core::version::version()),
            "version": SERVER_VERSION,
        },
        "instructions": INSTRUCTIONS,
    })
}

fn text_result(text: String, is_error: bool) -> Value {
    json!({"content": [{"type": "text", "text": text}], "isError": is_error})
}

fn call_tool(params: &Value) -> Value {
    let name = params["name"].as_str().unwrap_or_default();
    let Some(spec) = tool(name) else {
        return text_result(format!("Unknown tool: {name}"), true);
    };
    let empty = Value::Object(Map::new());
    let args = match &params["arguments"] {
        Value::Null => &empty,
        other => other,
    };
    let args = match validate(name, &spec["inputSchema"], args) {
        Ok(args) => args,
        Err(message) => {
            return text_result(format!("Error executing tool {name}: {message}"), true)
        }
    };
    let output = project(
        &run_tool(name, args),
        &spec["outputSchema"],
        &spec["outputSchema"],
    );
    let text = serde_json::to_string_pretty(&output).unwrap_or_default();
    json!({
        "content": [{"type": "text", "text": text}],
        "structuredContent": output,
        "isError": false,
    })
}

// ---------------------------------------------------------- validation ---

/// Python `repr` of a JSON value, as pydantic prints `input_value`.
fn py_repr(v: &Value) -> String {
    match v {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => match (n.as_i64(), n.as_f64()) {
            (Some(i), _) => i.to_string(),
            (None, Some(f)) if f.fract() == 0.0 && f.abs() < 1e16 => format!("{f:.1}"),
            _ => n.to_string(),
        },
        Value::String(s) => {
            if s.contains('\'') && !s.contains('"') {
                format!("\"{s}\"")
            } else {
                format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
            }
        }
        Value::Array(a) => format!("[{}]", a.iter().map(py_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}: {}", py_repr(&Value::String(k.clone())), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

fn py_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

struct Issue {
    field: String,
    message: &'static str,
    kind: &'static str,
    input: Value,
}

/// pydantic lax-mode coercion to `int`.
fn as_int(v: &Value) -> Result<Value, (&'static str, &'static str)> {
    match v {
        Value::Number(n) if n.is_i64() || n.is_u64() => Ok(v.clone()),
        Value::Number(n) => match n.as_f64() {
            Some(f) if f.fract() == 0.0 => Ok(json!(f as i64)),
            _ => Err((
                "Input should be a valid integer, got a number with a fractional part",
                "int_from_float",
            )),
        },
        Value::Bool(b) => Ok(json!(i64::from(*b))),
        Value::String(s) => s.trim().parse::<i64>().map(|i| json!(i)).map_err(|_| {
            (
                "Input should be a valid integer, unable to parse string as an integer",
                "int_parsing",
            )
        }),
        _ => Err(("Input should be a valid integer", "int_type")),
    }
}

/// Checks `args` against a tool's input schema the way the reference's
/// pydantic argument model does, returning the coerced arguments.
fn validate(tool: &str, schema: &Value, args: &Value) -> Result<Map<String, Value>, String> {
    let Value::Object(given) = args else {
        return Err(format!(
            "1 validation error for {tool}Arguments\n  Input should be a valid dictionary or \
             instance of {tool}Arguments [type=model_type, input_value={}, input_type={}]\n    \
             For further information visit {PYDANTIC_DOCS}/model_type",
            py_repr(args),
            py_type(args)
        ));
    };
    let required: Vec<&str> = schema["required"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .collect();
    let mut out = Map::new();
    let mut issues = Vec::new();
    for (field, prop) in schema["properties"].as_object().into_iter().flatten() {
        let Some(value) = given.get(field) else {
            if required.contains(&field.as_str()) {
                issues.push(Issue {
                    field: field.clone(),
                    message: "Field required",
                    kind: "missing",
                    input: args.clone(),
                });
            }
            continue;
        };
        let nullable = prop["anyOf"]
            .as_array()
            .is_some_and(|a| a.iter().any(|t| t["type"] == "null"));
        let ty = prop["type"].as_str().or_else(|| {
            prop["anyOf"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|t| t["type"].as_str())
                .find(|t| *t != "null")
        });
        if value.is_null() && nullable {
            out.insert(field.clone(), Value::Null);
            continue;
        }
        let checked = match ty {
            Some("string") => match value {
                Value::String(_) => Ok(value.clone()),
                _ => Err(("Input should be a valid string", "string_type")),
            },
            Some("integer") => as_int(value),
            _ => Ok(value.clone()),
        };
        match checked {
            Ok(v) => {
                out.insert(field.clone(), v);
            }
            Err((message, kind)) => issues.push(Issue {
                field: field.clone(),
                message,
                kind,
                input: value.clone(),
            }),
        }
    }
    if issues.is_empty() {
        return Ok(out);
    }
    let mut text = format!(
        "{} validation error{} for {tool}Arguments",
        issues.len(),
        if issues.len() == 1 { "" } else { "s" }
    );
    for i in &issues {
        text.push_str(&format!(
            "\n{}\n  {} [type={}, input_value={}, input_type={}]\n    For further information visit {PYDANTIC_DOCS}/{}",
            i.field,
            i.message,
            i.kind,
            py_repr(&i.input),
            py_type(&i.input),
            i.kind
        ));
    }
    Err(text)
}

// ---------------------------------------------------------- projection ---

fn resolve<'a>(schema: &'a Value, root: &'a Value) -> &'a Value {
    match schema["$ref"]
        .as_str()
        .and_then(|r| r.strip_prefix("#/$defs/"))
    {
        Some(name) => &root["$defs"][name],
        None => schema,
    }
}

/// Shapes `value` like the reference's pydantic output model: only the
/// schema's fields, defaults for the missing ones.
fn project(value: &Value, schema: &Value, root: &Value) -> Value {
    let schema = resolve(schema, root);
    if let Some(options) = schema["anyOf"].as_array() {
        if value.is_null() {
            return Value::Null;
        }
        return options
            .iter()
            .map(|o| resolve(o, root))
            .find(|o| o["type"] != "null")
            .map_or_else(|| value.clone(), |o| project(value, o, root));
    }
    match (schema["type"].as_str(), value) {
        (Some("object"), Value::Object(map)) => {
            let Some(props) = schema["properties"].as_object() else {
                return value.clone();
            };
            let mut out = Map::new();
            for (field, prop) in props {
                let v = match map.get(field) {
                    Some(v) => project(v, prop, root),
                    None if prop.get("default").is_some() => prop["default"].clone(),
                    None if resolve(prop, root)["type"] == "array" => json!([]),
                    None => continue,
                };
                out.insert(field.clone(), v);
            }
            Value::Object(out)
        }
        (Some("array"), Value::Array(items)) => Value::Array(
            items
                .iter()
                .map(|i| project(i, &schema["items"], root))
                .collect(),
        ),
        _ => value.clone(),
    }
}

// --------------------------------------------------------------- tools ---

/// The reference's exception class name for an error kind.
fn error_type(kind: ErrorKind) -> &'static str {
    match kind {
        ErrorKind::Generic => "RagMonkError",
        ErrorKind::Usage => "UsageError",
        ErrorKind::Config => "ConfigError",
        ErrorKind::SourceUnavailable => "SourceUnavailableError",
        ErrorKind::Database => "DatabaseError",
        ErrorKind::IndexingPartialFailure => "IndexingPartialFailureError",
        ErrorKind::HealthCheck => "HealthCheckError",
        ErrorKind::SecurityViolation => "SecurityViolationError",
        ErrorKind::RunLockTimeout => "RunLockTimeoutError",
        ErrorKind::LocalStorageModeRequired => "LocalStorageModeRequiredError",
        ErrorKind::ContentChangedDuringProcessing => "ContentChangedDuringProcessingError",
    }
}

fn failure(kind: &str, message: impl Into<String>) -> Value {
    json!({
        "schema_version": SCHEMA_VERSION,
        "ok": false,
        "error": {"type": kind, "message": message.into()},
    })
}

fn run_tool(name: &str, args: Map<String, Value>) -> Value {
    let home = match prepared_home() {
        Ok(h) => h,
        Err(e) => return failure(error_type(e.kind()), e.message()),
    };
    let timeout = match load(&home) {
        Ok(cfg) => cfg.mcp.request_timeout_seconds,
        Err(e) => return failure(error_type(e.kind()), e.message()),
    };
    let (tx, rx) = mpsc::channel();
    let name = name.to_owned();
    // An abandoned worker runs to completion in the background; the
    // timeout only stops waiting for it (same contract as the reference).
    std::thread::spawn(move || {
        let outcome = std::panic::catch_unwind(|| work(&home, &name, &args));
        let _ = tx.send(outcome);
    });
    let wait = Duration::try_from_secs_f64(timeout.max(0.0)).unwrap_or(Duration::MAX);
    match rx.recv_timeout(wait) {
        Ok(Ok(Ok(Value::Object(mut data)))) => {
            data.insert("schema_version".into(), json!(SCHEMA_VERSION));
            data.insert("ok".into(), json!(true));
            data.insert("error".into(), Value::Null);
            Value::Object(data)
        }
        Ok(Ok(Ok(_))) => failure("internal_error", "tool returned no data"),
        Ok(Ok(Err(e))) => failure(error_type(e.kind()), e.message()),
        Ok(Err(panic)) => failure(
            "internal_error",
            panic
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| panic.downcast_ref::<&str>().map(|s| (*s).to_owned()))
                .unwrap_or_else(|| "internal error".into()),
        ),
        Err(mpsc::RecvTimeoutError::Timeout) => failure(
            "timeout",
            format!(
                "tool call exceeded mcp.request_timeout_seconds={}s",
                py_float(timeout)
            ),
        ),
        Err(mpsc::RecvTimeoutError::Disconnected) => failure("internal_error", "worker stopped"),
    }
}

/// Python `str(float)` for the timeout message.
fn py_float(f: f64) -> String {
    if f.fract() == 0.0 && f.abs() < 1e16 {
        format!("{f:.1}")
    } else {
        f.to_string()
    }
}

fn required(args: &Map<String, Value>, field: &str) -> Result<String, RagMonkError> {
    let value = args[field].as_str().unwrap_or_default().trim();
    if value.is_empty() {
        return Err(RagMonkError::usage(format!("{field} must not be empty")));
    }
    Ok(value.to_owned())
}

/// An optional positive integer argument; `None`/0 mean the default.
fn optional(args: &Map<String, Value>, field: &str, default: usize) -> usize {
    match args.get(field).and_then(Value::as_i64) {
        Some(v) if v != 0 => v.max(0) as usize,
        _ => default,
    }
}

fn work(home: &Home, name: &str, args: &Map<String, Value>) -> Result<Value, RagMonkError> {
    let cfg = load(home)?;
    match name {
        "ragmonk_explore" => {
            let query = required(args, "query")?;
            let mut budget = query_cmd::config_budget(&cfg);
            let set = |field: &str, slot: &mut usize| {
                if let Some(v) = args.get(field).and_then(Value::as_i64) {
                    *slot = v.max(0) as usize;
                }
            };
            set("max_chars", &mut budget.max_chars);
            set("max_files", &mut budget.max_files);
            set("max_graph_nodes", &mut budget.max_graph_nodes);
            let opened = open_sources(home, None)?;
            let r = query_cmd::explore_value(home, &cfg, &opened, &query, budget)?;
            let mut warnings: Vec<Value> = Vec::new();
            if r["evidence_truncated"] == true {
                warnings.extend(
                    r["evidence_truncation_reasons"]
                        .as_array()
                        .cloned()
                        .unwrap_or_default(),
                );
            }
            let empty = |k: &str| r[k].as_array().is_none_or(Vec::is_empty);
            if empty("requirements") && empty("incidents") {
                warnings.push(json!(
                    "Requirements/Incidents are always empty: RagMonk has no document \
                     classifier yet to populate these categories (see cli/explore.py)."
                ));
            }
            Ok(json!({
                "query": r["query"],
                "intent": r["intent"],
                "strategies": r["strategies"],
                "answer_context": r["summary"],
                "entities": r["symbols"],
                "relationships": r["call_flows"],
                "documents": r["documents"],
                "tests": r["tests"],
                "evidence": r["evidence"],
                "warnings": warnings,
            }))
        }
        "ragmonk_search" => {
            let query = required(args, "query")?;
            let limit = optional(args, "limit", lexical::DEFAULT_LIMIT);
            query_cmd::lexical_value(&cfg, &open_sources(home, None)?, &query, limit)
        }
        "ragmonk_symbol" => {
            let name = required(args, "name")?;
            query_cmd::symbol_value(&open_sources(home, None)?, &name)
        }
        "ragmonk_callers" | "ragmonk_callees" => {
            let symbol = required(args, "name")?;
            let direction = if name == "ragmonk_callers" {
                Direction::Incoming
            } else {
                Direction::Outgoing
            };
            query_cmd::calls_value(
                &open_sources(home, None)?,
                &symbol,
                direction,
                optional(args, "max_depth", graph::DEFAULT_MAX_DEPTH),
                optional(args, "limit", graph::DEFAULT_LIMIT),
            )
        }
        "ragmonk_impact" => {
            let symbol = required(args, "name")?;
            query_cmd::impact_value(
                &open_sources(home, None)?,
                &symbol,
                optional(args, "max_depth", graph::DEFAULT_MAX_DEPTH),
                optional(args, "limit", graph::DEFAULT_LIMIT),
            )
        }
        "ragmonk_documents" => {
            let source = args
                .get("source_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|s| !s.is_empty());
            Ok(json!({"documents": workflow::docs_rows(home, source)?}))
        }
        "ragmonk_status" => status_cmd::collect(home),
        other => Err(RagMonkError::usage(format!("Unknown tool: {other}"))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_has_the_eight_read_only_tools() {
        let names: Vec<&str> = tools().iter().filter_map(|t| t["name"].as_str()).collect();
        assert_eq!(names.len(), 8);
        assert!(!names.contains(&"ragmonk_ask"));
        assert!(!INSTRUCTIONS.contains("ragmonk_ask"));
    }

    #[test]
    fn validation_matches_pydantic() {
        let schema = &tool("ragmonk_search").unwrap()["inputSchema"];
        let err = validate("ragmonk_search", schema, &json!({"limit": "x"})).unwrap_err();
        assert!(err.starts_with("2 validation errors for ragmonk_searchArguments\nquery\n  Field required [type=missing, input_value={'limit': 'x'}, input_type=dict]"), "{err}");
        assert!(err.contains("limit\n  Input should be a valid integer, unable to parse string as an integer [type=int_parsing, input_value='x', input_type=str]"), "{err}");
        let ok = validate(
            "ragmonk_search",
            schema,
            &json!({"query": "q", "limit": "3", "extra": 1}),
        )
        .unwrap();
        assert_eq!(ok["limit"], 3);
        assert!(!ok.contains_key("extra"));
    }

    #[test]
    fn projection_keeps_schema_fields_with_defaults() {
        let schema = &tool("ragmonk_documents").unwrap()["outputSchema"];
        let v = project(
            &json!({"ok": true, "documents": [{"source_id": "s", "file_id": "f", "path": "p", "status": "indexed", "section_count": 1, "paragraph_count": 0, "table_count": 0, "is_scanned": false, "attachments": []}]}),
            schema,
            schema,
        );
        assert_eq!(v["schema_version"], "1");
        assert!(v["error"].is_null());
        assert!(v["documents"][0].get("attachments").is_none());
        assert!(v["documents"][0]["title"].is_null());
    }

    #[test]
    fn protocol_basics() {
        assert!(handle_line(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#).is_none());
        let r = handle_line(r#"{"jsonrpc":"2.0","id":7,"method":"initialize","params":{"protocolVersion":"1999-01-01"}}"#).unwrap();
        assert_eq!(r["result"]["protocolVersion"], LATEST_PROTOCOL_VERSION);
        let r = handle_line(r#"{"jsonrpc":"2.0","id":"a","method":"x"}"#).unwrap();
        assert_eq!(r["error"]["code"], -32602);
        assert_eq!(r["id"], "a");
        let r = handle_line("garbage").unwrap();
        assert_eq!(r["method"], "notifications/message");
    }
}
