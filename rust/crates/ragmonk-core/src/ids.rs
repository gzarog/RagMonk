//! Stable identifier algorithms.
//!
//! * source IDs: `src_` + sha256(canonical path)[:10] (`sources.registry`);
//! * project IDs: sha256(resolved path)[:12] (`core.paths`, see
//!   [`crate::paths::project_id_for_path`]);
//! * server document `_id`s: sha1 over `\x1f`-joined parts. The Python
//!   OpenSearch and Elasticsearch modules are deliberately independent but
//!   byte-identical; golden tests verify this single port against both.
//!
//! Local-mode file/entity/document/chunk/job IDs are random `uuid4().hex`
//! in the reference (ADR 0001) and are not derived here.

use sha1::Sha1;
use sha2::{Digest, Sha256};

use crate::models::SourceType;

const SEP: &str = "\x1f";

pub fn sha256_hex(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(out, "{b:02x}");
    }
    out
}

pub fn make_source_id(canonical_path: &str) -> String {
    format!("src_{}", &sha256_hex(canonical_path.as_bytes())[..10])
}

const NETWORK_PREFIXES: &[&str] = &["\\\\", "//", "smb://", "nfs://", "afp://"];

/// `detect_source_type` on the *raw* user-supplied path.
pub fn detect_source_type(raw_path: &str) -> SourceType {
    if NETWORK_PREFIXES.iter().any(|p| raw_path.starts_with(p)) && !raw_path.starts_with("///") {
        SourceType::Network
    } else {
        SourceType::Local
    }
}

fn digest(parts: &[&str]) -> String {
    hex(&Sha1::digest(parts.join(SEP).as_bytes()))
}

fn with_generation<'a>(mut parts: Vec<&'a str>, generation: Option<&'a str>) -> Vec<&'a str> {
    if let Some(generation) = generation {
        parts.push("gen");
        parts.push(generation);
    }
    parts
}

/// Deterministic server-backend document IDs.
pub mod server {
    use super::*;

    pub fn file_doc_id(source_id: &str, file_id: &str, generation: Option<&str>) -> String {
        digest(&with_generation(
            vec!["file", source_id, file_id],
            generation,
        ))
    }

    pub fn generation_marker_id(source_id: &str) -> String {
        digest(&["generation-marker", source_id])
    }

    pub fn document_doc_id(
        source_id: &str,
        file_id: &str,
        generation: Option<&str>,
        attachment_index: Option<u64>,
    ) -> String {
        let index = attachment_index.map(|i| i.to_string());
        let mut parts = vec!["document", source_id, file_id];
        if let Some(index) = index.as_deref() {
            parts.push("attachment");
            parts.push(index);
        }
        digest(&with_generation(parts, generation))
    }

    pub fn entity_doc_id(source_id: &str, file_id: &str, entity_id: &str) -> String {
        digest(&["entity", source_id, file_id, entity_id])
    }

    pub fn chunk_doc_id(source_id: &str, file_id: &str, chunk_id: &str) -> String {
        digest(&["chunk", source_id, file_id, chunk_id])
    }

    pub fn relationship_doc_id(source_id: &str, file_id: &str, relationship_id: &str) -> String {
        digest(&["relationship", source_id, file_id, relationship_id])
    }

    pub fn link_doc_id(
        source_id: &str,
        entity_id: &str,
        document_id: &str,
        section_id: Option<&str>,
        link_type: &str,
        resolver: &str,
        generation: Option<&str>,
    ) -> String {
        digest(&with_generation(
            vec![
                "link",
                source_id,
                entity_id,
                document_id,
                section_id.unwrap_or(""),
                link_type,
                resolver,
            ],
            generation,
        ))
    }
}
