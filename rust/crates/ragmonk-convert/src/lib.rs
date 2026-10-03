//! Rust-native document ingestion (RUST-07): format routing, the
//! `DocumentConverter` seam backed by docling.rs, the Docling-JSON
//! normalizer and the V2 document processor.

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

/// The full V2 registry: Rust-native code extraction plus Rust-native
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
}

impl RegistryOptions {
    /// `<home>/models/ocrs` and `<home>/v2/cache/conversion`.
    pub fn for_home(home: &ragmonk_core::paths::Home) -> Self {
        Self {
            ocr_models_dir: Some(home.root().join("models").join("ocrs")),
            cache_dir: Some(
                ragmonk_storage::V2Layout::new(home)
                    .root()
                    .join("cache")
                    .join("conversion"),
            ),
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
    let mut r = ragmonk_code::registry();
    // Toggling attachment extraction changes what an .eml yields, so it is
    // part of the identity (the reference's `+eml-attachments.1`).
    r.versions.converter_version = if d.email_attachments {
        format!(
            "{}+{}",
            converter.parser_version(),
            ragmonk_documents::version::EMAIL_ATTACHMENTS_VERSION
        )
    } else {
        converter.parser_version()
    };
    r.versions.chunker_version = ragmonk_documents::chunker::chunker_version_stamp();
    r.versions.embedding_text_version =
        Some(ragmonk_documents::chunker::EMBEDDING_TEXT_VERSION.into());
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
