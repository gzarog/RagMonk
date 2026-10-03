//! Rust-native document ingestion (RUST-07): format routing, the
//! `DocumentConverter` seam backed by docling.rs, the Docling-JSON
//! normalizer and the V2 document processor.

pub mod converter;
pub mod format;
pub mod normalize;
pub mod process;

use std::sync::Arc;

use ragmonk_config::RagMonkConfig;
use ragmonk_indexing::coordinator::Registry;

/// The full V2 registry: Rust-native code extraction plus Rust-native
/// document conversion and chunking. Version identities make any change in
/// the converter, chunker or tokenizer force a full rebuild.
pub fn registry(config: &RagMonkConfig) -> Registry {
    let converter: Arc<dyn converter::DocumentConverter> = Arc::new(converter::DoclingConverter);
    let mut r = ragmonk_code::registry();
    r.versions.converter_version = converter.parser_version();
    r.versions.chunker_version = ragmonk_documents::chunker::chunker_version_stamp();
    r.versions.embedding_text_version =
        Some(ragmonk_documents::chunker::EMBEDDING_TEXT_VERSION.into());
    r.document = Arc::new(process::DocumentProcessor {
        converter,
        chunking: config.documents.chunking.clone(),
    });
    r
}
