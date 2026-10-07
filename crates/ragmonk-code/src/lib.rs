//! Tree-sitter code intelligence (RUST-05): language detection, the
//! reference's query-driven extractor, reference resolution, framework
//! heuristics, the code processor for the V2 indexer and graph traversal.

pub mod extract;
pub mod framework;
pub mod graph;
pub mod lang;
pub mod process;
pub mod resolve;

use std::sync::Arc;

use ragmonk_indexing::coordinator::{RawProcessor, Registry};

/// The V2 registry with Rust-native code extraction (documents stay raw
/// until RUST-07).
pub fn registry() -> Registry {
    let mut r = Registry::raw();
    r.code = Arc::new(process::CodeProcessor);
    r.document = Arc::new(RawProcessor);
    r.finalizers.push(Arc::new(process::CrossFileResolver));
    r.versions.parser_version = process::CODE_DERIVATION_VERSION.into();
    r
}
