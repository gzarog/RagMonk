//! `ask`: an AI answer grounded in the evidence `explore` retrieves.

use ragmonk_ai::AiRequest;
use ragmonk_core::errors::RagMonkError;
use serde_json::{json, Value};

use crate::load;
use crate::query::{config_budget, explore_value, open_sources};

/// `ask` data: the answer plus the evidence behind it.
pub fn ask_value(question: &str) -> Result<Value, RagMonkError> {
    let home = crate::prepared_home()?;
    let cfg = load(&home)?;
    let question = question.trim();
    let opened = open_sources(&home, None)?;
    let r = explore_value(&home, &cfg, &opened, question, config_budget(&cfg))?;
    let mut provider = ragmonk_ai::create_provider(&cfg.ai, &cfg.privacy)?;
    let list = |k: &str| r[k].as_array().cloned().unwrap_or_default();
    let request = AiRequest {
        question: question.to_owned(),
        summary: r["summary"].as_str().unwrap_or_default().to_owned(),
        evidence: list("evidence"),
        graph_paths: list("call_flows"),
    };
    let answer = provider.answer(&request)?;
    Ok(json!({
        "question": question,
        "answer": answer.to_json(),
        "intent": r["intent"],
        "strategies": r["strategies"],
        "evidence": r["evidence"],
        "graph_paths": r["call_flows"],
        "evidence_truncated": r["evidence_truncated"],
        "evidence_truncation_reasons": r["evidence_truncation_reasons"],
    }))
}
