//! The `DocumentConverter` seam and its docling.rs implementation.

use std::path::Path;

use serde_json::Value;

use crate::format::DocumentFormat;

/// Converter identity, part of `parser_version` (a change rebuilds every
/// source's documents).
pub const DOCLING_PARSER_VERSION: &str = "docling.rs-1.89.0";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConversionError {
    /// Recognized but not converted by this build (e.g. PDF/EML/images
    /// before their slices land); indexed with no derived content.
    #[error("{0} conversion is not available")]
    Unsupported(&'static str),
    #[error("conversion failed: {0}")]
    Failed(String),
}

/// Converts a file into Docling document JSON (the schema the normalizer
/// consumes). Implementations must not touch storage or the network.
pub trait DocumentConverter: Send + Sync {
    fn convert(&self, path: &Path, format: DocumentFormat) -> Result<Value, ConversionError>;
    fn parser_version(&self) -> String;
}

/// Rule-based docling.rs backends (no ML, no network).
pub struct DoclingConverter;

impl DocumentConverter for DoclingConverter {
    fn convert(&self, path: &Path, format: DocumentFormat) -> Result<Value, ConversionError> {
        use docling::InputFormat as F;
        let input = match format {
            DocumentFormat::Docx => F::Docx,
            DocumentFormat::Pptx => F::Pptx,
            DocumentFormat::Xlsx => F::Xlsx,
            DocumentFormat::Html => F::Html,
            // The reference routes plain text through the Markdown backend.
            DocumentFormat::Markdown | DocumentFormat::Txt => F::Md,
            DocumentFormat::Csv => F::Csv,
            DocumentFormat::Odt => F::Odt,
            DocumentFormat::Ods => F::Ods,
            DocumentFormat::Odp => F::Odp,
            DocumentFormat::Epub => F::Epub,
            DocumentFormat::Pdf | DocumentFormat::Eml | DocumentFormat::Image => {
                return Err(ConversionError::Unsupported(format.as_str()))
            }
        };
        let bytes = std::fs::read(path).map_err(|e| ConversionError::Failed(e.to_string()))?;
        let name = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("document")
            .to_owned();
        let source = docling::SourceDocument::from_bytes(name, input, bytes);
        // Backends may panic on adversarial input; contain it per file.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            docling::DocumentConverter::new().convert(source)
        }))
        .map_err(|_| ConversionError::Failed("converter panicked".into()))?
        .map_err(|e| ConversionError::Failed(e.to_string()))?;
        Ok(result.document.export_to_json_value())
    }

    fn parser_version(&self) -> String {
        DOCLING_PARSER_VERSION.into()
    }
}
