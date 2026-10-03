//! Embedding vectors and the cross-build embedding cache (RUST-09).
//!
//! Vectors are build-scoped rows keyed by subject (`entity` or `chunk`) and
//! stamped with the model fingerprint. Anything in a build that lacks a
//! vector under the current fingerprint is "pending"; that single rule
//! covers touched files (their rows were replaced), crash recovery (a
//! missing vector is simply pending again) and model changes (every row
//! carries the old fingerprint, so everything is re-embedded explicitly).

use std::collections::HashMap;

use rusqlite::params;

use crate::db::{now_iso, write_tx};
use crate::error::{Result, StorageError};
use crate::knowledge::{cached, ProjectStore};

/// Subject kinds stored in `embeddings.subject_type`.
pub const SUBJECT_ENTITY: &str = "entity";
pub const SUBJECT_CHUNK: &str = "chunk";

/// Something that still needs a vector under the current model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingSubject {
    pub subject_type: &'static str,
    pub subject_id: String,
    pub file_id: String,
    /// The exact text handed to the model.
    pub text: String,
}

/// A computed vector ready to be written.
#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingRow {
    pub subject_type: &'static str,
    pub subject_id: String,
    pub file_id: String,
    pub text_hash: String,
    pub text_version: String,
    pub vector: Vec<f32>,
}

/// A stored vector as read back for search.
#[derive(Debug, Clone, PartialEq)]
pub struct StoredEmbedding {
    pub subject_type: String,
    pub subject_id: String,
    pub file_id: String,
    pub model_fingerprint: String,
    pub vector: Vec<f32>,
}

/// Little-endian f32 encoding used for every vector blob.
pub fn encode_vector(v: &[f32]) -> Vec<u8> {
    v.iter().flat_map(|x| x.to_le_bytes()).collect()
}

pub fn decode_vector(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

impl ProjectStore {
    /// Entities (`signature`, else `qualified_name`) and chunks
    /// (`embedding_text`, else `text`) of `build_id` that have no vector
    /// under `fingerprint`, in a stable order. Blank texts are skipped.
    pub fn pending_embedding_subjects(
        &self,
        build_id: &str,
        fingerprint: &str,
    ) -> Result<Vec<EmbeddingSubject>> {
        let mut out = self.query_rows(
            "pending entity embeddings",
            "SELECT e.id, e.file_id, COALESCE(NULLIF(e.signature, ''), e.qualified_name)
             FROM entities e
             LEFT JOIN embeddings v ON v.build_id = e.build_id AND v.subject_type = 'entity'
                AND v.subject_id = e.id AND v.model_fingerprint = ?2
             WHERE e.build_id = ?1 AND v.subject_id IS NULL
             ORDER BY e.file_id, e.id",
            &[&build_id, &fingerprint],
            |r| {
                Ok(EmbeddingSubject {
                    subject_type: SUBJECT_ENTITY,
                    subject_id: r.get(0)?,
                    file_id: r.get(1)?,
                    text: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                })
            },
        )?;
        out.extend(self.query_rows(
            "pending chunk embeddings",
            "SELECT c.id, c.file_id, COALESCE(NULLIF(c.embedding_text, ''), c.text)
             FROM chunks c
             LEFT JOIN embeddings v ON v.build_id = c.build_id AND v.subject_type = 'chunk'
                AND v.subject_id = c.id AND v.model_fingerprint = ?2
             WHERE c.build_id = ?1 AND v.subject_id IS NULL
             ORDER BY c.file_id, c.ordinal, c.id",
            &[&build_id, &fingerprint],
            |r| {
                Ok(EmbeddingSubject {
                    subject_type: SUBJECT_CHUNK,
                    subject_id: r.get(0)?,
                    file_id: r.get(1)?,
                    text: r.get(2)?,
                })
            },
        )?);
        out.retain(|s| !s.text.trim().is_empty());
        Ok(out)
    }

    /// Number of vectors in `build_id` stamped with a different model.
    pub fn stale_embedding_count(&self, build_id: &str, fingerprint: &str) -> Result<i64> {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM embeddings WHERE build_id = ?1 AND model_fingerprint != ?2",
                params![build_id, fingerprint],
                |r| r.get(0),
            )
            .map_err(StorageError::sqlite("stale embeddings"))
    }

    /// Drops vectors of `build_id` that were produced by another model.
    pub fn delete_stale_embeddings(&mut self, build_id: &str, fingerprint: &str) -> Result<usize> {
        write_tx(&mut self.conn, |tx| {
            cached(
                tx,
                "DELETE FROM embeddings WHERE build_id = ?1 AND model_fingerprint != ?2",
                params![build_id, fingerprint],
            )
            .map_err(StorageError::sqlite("delete stale embeddings"))
        })
    }

    /// Cache lookup by exact `(text_hash, text_version)` under `fingerprint`.
    pub fn cached_embeddings(
        &self,
        fingerprint: &str,
        keys: &[(String, String)],
    ) -> Result<HashMap<(String, String), Vec<f32>>> {
        let mut stmt = self
            .conn
            .prepare_cached(
                "SELECT vector FROM embedding_cache
                 WHERE text_hash = ?1 AND model_fingerprint = ?2 AND embedding_text_version = ?3",
            )
            .map_err(StorageError::sqlite("embedding cache"))?;
        let mut out = HashMap::new();
        for (hash, version) in keys {
            let blob: Option<Vec<u8>> = stmt
                .query_row(params![hash, fingerprint, version], |r| r.get(0))
                .map(Some)
                .or_else(|e| match e {
                    rusqlite::Error::QueryReturnedNoRows => Ok(None),
                    e => Err(e),
                })
                .map_err(StorageError::sqlite("embedding cache"))?;
            if let Some(blob) = blob {
                out.insert((hash.clone(), version.clone()), decode_vector(&blob));
            }
        }
        Ok(out)
    }

    /// Writes vectors for `build_id` and upserts them into the cache, in one
    /// transaction.
    pub fn put_embeddings(
        &mut self,
        build_id: &str,
        fingerprint: &str,
        rows: &[EmbeddingRow],
    ) -> Result<()> {
        let now = now_iso();
        write_tx(&mut self.conn, |tx| {
            for row in rows {
                let blob = encode_vector(&row.vector);
                let dims = row.vector.len() as i64;
                cached(
                    tx,
                    "INSERT OR REPLACE INTO embeddings (build_id, subject_type, subject_id, file_id,
                        model_fingerprint, text_hash, dims, vector)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                    params![
                        build_id,
                        row.subject_type,
                        row.subject_id,
                        row.file_id,
                        fingerprint,
                        row.text_hash,
                        dims,
                        blob
                    ],
                )
                .map_err(StorageError::sqlite("put embedding"))?;
                cached(
                    tx,
                    "INSERT OR IGNORE INTO embedding_cache (text_hash, model_fingerprint,
                        embedding_text_version, dims, vector, created_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![
                        row.text_hash,
                        fingerprint,
                        row.text_version,
                        dims,
                        blob,
                        now
                    ],
                )
                .map_err(StorageError::sqlite("put embedding cache"))?;
            }
            Ok(())
        })
    }

    /// Every vector of `build_id` (ordered by subject).
    pub fn embeddings(&self, build_id: &str) -> Result<Vec<StoredEmbedding>> {
        self.query_rows(
            "embeddings",
            "SELECT subject_type, subject_id, file_id, model_fingerprint, vector
             FROM embeddings WHERE build_id = ?1 ORDER BY subject_type, subject_id",
            &[&build_id],
            |r| {
                Ok(StoredEmbedding {
                    subject_type: r.get(0)?,
                    subject_id: r.get(1)?,
                    file_id: r.get(2)?,
                    model_fingerprint: r.get(3)?,
                    vector: decode_vector(&r.get::<_, Vec<u8>>(4)?),
                })
            },
        )
    }

    /// Rows in the persistent embedding cache.
    pub fn embedding_cache_len(&self) -> Result<i64> {
        self.conn
            .query_row("SELECT COUNT(*) FROM embedding_cache", [], |r| r.get(0))
            .map_err(StorageError::sqlite("embedding cache size"))
    }
}
