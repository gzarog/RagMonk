//! Rust-native document ingestion: format routing, the
//! `DocumentConverter` seam backed by docling.rs, the Docling-JSON
//! normalizer and the document processor.

pub mod cache;
pub mod converter;
pub mod email;
pub mod format;
pub mod normalize;
pub mod ocr;
pub mod pdf;
pub mod process;

use std::sync::Arc;

use ragmonk_config::RagMonkConfig;
use ragmonk_indexing::coordinator::Registry;

/// The full registry: Rust-native code extraction plus Rust-native
/// document conversion and chunking. Version identities make any change in
/// the converter, chunker or tokenizer force a full rebuild.
pub fn registry(config: &RagMonkConfig) -> Registry {
    registry_with(config, &RegistryOptions::default())
}

/// Where OCR models and the conversion cache live.
#[derive(Debug, Clone, Default)]
pub struct RegistryOptions {
    /// Default `<home>/models/ocrs` (overridden by `RAGMONK_OCR_MODELS_DIR`).
    pub ocr_models_dir: Option<std::path::PathBuf>,
    /// Default: no cache.
    pub cache_dir: Option<std::path::PathBuf>,
    /// Embedding models root. Default `<home>/models` (overridden by
    /// `RAGMONK_MODELS_DIR`).
    pub models_root: Option<std::path::PathBuf>,
}

impl RegistryOptions {
    /// `<home>/models/ocrs` and `<home>/cache/conversion`.
    pub fn for_home(home: &ragmonk_core::paths::Home) -> Self {
        Self {
            ocr_models_dir: Some(home.models_dir().join("ocrs")),
            models_root: Some(home.models_dir()),
            cache_dir: Some(home.cache_dir().join("conversion")),
        }
    }
}

pub fn registry_with(config: &RagMonkConfig, opts: &RegistryOptions) -> Registry {
    let d = &config.documents;
    let pdf = converter::PdfOptions {
        ocr: d.ocr.clone(),
        image_ocr: d.image_ocr,
        mode: d.pdf_mode.clone(),
    };
    let converter: Arc<dyn converter::DocumentConverter> = Arc::new(converter::DoclingConverter {
        pdf,
        ocr: Some(Arc::new(ocr::LazyOcr::new(ocr::models_dir(
            opts.ocr_models_dir.as_deref(),
        )))),
        cache: opts.cache_dir.clone().map(cache::ConversionCache::new),
    });
    let relationships = config.indexing.relationships_enabled;
    let mut r = ragmonk_code::registry_with_relationships(relationships);
    // Toggling attachment extraction changes what an .eml yields, so it is
    // part of the identity (`+eml-attachments.1`).
    r.versions.converter_version = if d.email_attachments {
        format!(
            "{}+{}",
            converter.parser_version(),
            ragmonk_documents::version::EMAIL_ATTACHMENTS_VERSION
        )
    } else {
        converter.parser_version()
    };
    r.versions.chunker_version = format!(
        "{}+{}",
        ragmonk_documents::chunker::chunker_version_stamp(),
        ragmonk_documents::version::CHUNK_PARENTS_VERSION
    );
    r.versions.embedding_text_version =
        Some(ragmonk_documents::chunker::EMBEDDING_TEXT_VERSION.into());
    // Cross-domain links run after cross-file resolution, over the files
    // this build wrote; manual links are re-applied to every build.
    if relationships {
        r.finalizers
            .push(Arc::new(ragmonk_knowledge::KnowledgeLinker));
    }
    // Semantic vectors: every entity/chunk without a vector under the
    // current model is embedded after linking. A missing model leaves them
    // pending (logged) instead of failing the build.
    if config.search.semantic {
        r.finalizers
            .push(Arc::new(ragmonk_ml::EmbeddingFinalizer::new(
                ragmonk_ml::LazyEmbedder::new(
                    ragmonk_ml::embedder::models_root(opts.models_root.as_deref()),
                    ragmonk_ml::manifest::DEFAULT_EMBEDDING_MODEL,
                    config.indexing.embedding_batch_size,
                ),
            )));
    }
    r.document = Arc::new(process::DocumentProcessor {
        converter,
        chunking: config.documents.chunking.clone(),
        max_pages: (d.max_pages > 0).then_some(d.max_pages as usize),
        attachments: Some(email::AttachmentSettings {
            enabled: d.email_attachments,
            max_bytes: d.email_attachment_max_bytes.max(0) as usize,
            max_count: d.email_attachment_max_count.max(0) as usize,
            total_max_bytes: d.email_attachment_total_max_bytes.max(0) as usize,
        }),
        image_ocr: d.image_ocr,
    });
    r
}
