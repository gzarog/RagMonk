//! The V2 document processor (`documents/pipeline.py`'s prepare half):
//! detect -> convert -> normalize -> metadata -> chunk, all on the worker,
//! with no storage access and no Python.

use std::path::Path;
use std::sync::Arc;

use ragmonk_config::model::ChunkingConfig;
use ragmonk_core::ids::v2;
use ragmonk_documents::chunker::chunk_document;
use ragmonk_documents::model::DocumentMetadata;
use ragmonk_indexing::coordinator::{PrepareInput, ProcessError, Processor};
use ragmonk_storage::knowledge::{ChunkRow, DocumentRow, FileKnowledge};

use crate::converter::{ConversionError, DocumentConverter};
use crate::format::detect_format;
use crate::normalize::normalize;

pub struct DocumentProcessor {
    pub converter: Arc<dyn DocumentConverter>,
    pub chunking: ChunkingConfig,
}

impl DocumentProcessor {
    /// Pure conversion of one file into its knowledge rows.
    pub fn knowledge(
        &self,
        path: &Path,
        file_id: &str,
        content_hash: Option<&str>,
    ) -> Result<FileKnowledge, ProcessError> {
        let Some(format) = detect_format(path) else {
            return Ok(FileKnowledge::default());
        };
        let json = match self.converter.convert(path, format) {
            Ok(j) => j,
            // Recognized but not convertible by this build: indexed with no
            // derived content, like the reference's unsupported formats.
            Err(ConversionError::Unsupported(_)) => return Ok(FileKnowledge::default()),
            Err(ConversionError::Failed(m)) => {
                return Err(ProcessError {
                    code: "conversion_error".into(),
                    message: format!("{}: {m}", path.display()),
                    transient: false,
                })
            }
        };
        let normalized = normalize(&json, format == crate::format::DocumentFormat::Pdf);
        let file_name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
        let meta = DocumentMetadata::from_normalized(&normalized, format.as_str(), file_name);
        let title = meta.title.clone().unwrap_or_default();
        let chunks = chunk_document(&normalized, &self.chunking, &title, None);
        let document_id = v2::document_id(file_id, None);
        let chunk_rows = chunks
            .into_iter()
            .enumerate()
            .map(|(i, c)| ChunkRow {
                id: v2::chunk_id(&document_id, i as u64),
                document_id: document_id.clone(),
                file_id: file_id.to_owned(),
                kind: c.kind.as_str().to_owned(),
                ordinal: i as i64,
                heading_path: c.heading_path,
                heading_level: c.heading_level,
                text: c.text,
                search_text: c.search_text,
                embedding_text: Some(c.contextual_text),
                token_count: Some(c.token_count),
                page_start: c.page_start,
                page_end: c.page_end,
                table_rows: c.table_rows,
                caption: c.caption,
            })
            .collect();
        Ok(FileKnowledge {
            documents: vec![DocumentRow {
                id: document_id,
                file_id: file_id.to_owned(),
                format: format.as_str().to_owned(),
                title: meta.title,
                author: meta.author,
                page_count: meta.page_count,
                is_scanned: meta.is_scanned,
                content_hash: content_hash.map(str::to_owned),
                attachment: None,
            }],
            chunks: chunk_rows,
            ..FileKnowledge::default()
        })
    }
}

impl Processor for DocumentProcessor {
    fn prepare(&self, input: &PrepareInput) -> Result<FileKnowledge, ProcessError> {
        self.knowledge(&input.path, &input.file_id, input.content_hash.as_deref())
    }
}
