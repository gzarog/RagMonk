//! The request/answer shapes and the one shared evidence prompt
//! so every provider grounds its answer in identical
//! context.

use serde_json::{json, Map, Value};

use crate::text::plain;

pub const SYSTEM_PROMPT: &str = "You are RagMonk's evidence-grounded assistant. Answer the user's \
question using ONLY the evidence and call-graph paths provided below -- they were retrieved \
deterministically from the user's own locally indexed code and documents, not written by you. If \
the evidence does not answer the question, say so plainly rather than guessing. Cite the relevant \
file paths from the evidence when you use them.";

/// The question plus the evidence `explore` assembled for it
/// (`evidence`/`graph_paths` are explore's own JSON items).
#[derive(Debug, Clone, Default)]
pub struct AiRequest {
    pub question: String,
    pub summary: String,
    pub evidence: Vec<Value>,
    pub graph_paths: Vec<Value>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AiUsage {
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AiAnswer {
    pub text: String,
    pub provider: String,
    pub model: String,
    pub usage: AiUsage,
}

impl AiAnswer {
    pub fn to_json(&self) -> Value {
        json!({
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
            },
        })
    }
}

/// An evidence location's keys in a fixed documented order
/// (`EvidenceLocation.to_dict`), then any others.
fn location_entries(loc: &Map<String, Value>) -> Vec<(&String, &Value)> {
    const ORDER: [&str; 4] = ["line_start", "line_end", "page", "section"];
    let mut out: Vec<(&String, &Value)> =
        ORDER.iter().filter_map(|k| loc.get_key_value(*k)).collect();
    out.extend(loc.iter().filter(|(k, _)| !ORDER.contains(&k.as_str())));
    out
}

fn field(item: &Value, key: &str) -> String {
    plain(item.get(key).unwrap_or(&Value::Null))
}

/// The provider-agnostic evidence prompt (`build_prompt`).
pub fn build_prompt(request: &AiRequest) -> String {
    let mut lines = vec![
        format!("Question: {}", request.question),
        String::new(),
        format!("Retrieval summary: {}", request.summary),
        String::new(),
    ];
    if !request.evidence.is_empty() {
        lines.push("Evidence:".into());
        for item in &request.evidence {
            let empty = Map::new();
            let location = item
                .get("location")
                .and_then(Value::as_object)
                .unwrap_or(&empty);
            let where_ = location_entries(location)
                .into_iter()
                .filter(|(_, v)| !v.is_null())
                .map(|(k, v)| format!("{k}={}", plain(v)))
                .collect::<Vec<_>>()
                .join(", ");
            let snippet = item.get("snippet").map(plain).unwrap_or_default();
            lines.push(format!(
                "- [{}] {} ({}{}): {}",
                field(item, "confidence"),
                field(item, "entity"),
                field(item, "path"),
                if where_.is_empty() {
                    String::new()
                } else {
                    format!(", {where_}")
                },
                snippet
            ));
        }
        lines.push(String::new());
    }
    if !request.graph_paths.is_empty() {
        lines.push("Call graph:".into());
        for edge in &request.graph_paths {
            lines.push(format!(
                "- {} -[{}]-> {}",
                field(edge, "source"),
                field(edge, "relationship"),
                field(edge, "target")
            ));
        }
        lines.push(String::new());
    }
    if request.evidence.is_empty() && request.graph_paths.is_empty() {
        lines.push("No evidence was retrieved for this question.".into());
    }
    lines.join("\n")
}

/// The same prompt as chat messages (`build_messages`).
pub fn build_messages(request: &AiRequest) -> Value {
    json!([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_prompt(request)},
    ])
}
