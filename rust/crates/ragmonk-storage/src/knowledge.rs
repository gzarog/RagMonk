//! Per-source V2 knowledge store (`<home>/v2/projects/<id>/knowledge.db`).
//!
//! All index-derived rows belong to a build. Readers pass the source's
//! active build id; rows of a building/aborted build are invisible. Writes
//! for one file are a single short transaction (callers prepare/convert/
//! embed before calling, never inside).

use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde::Serialize;

use crate::control::{IndexVersions, RebuildPlan};
use crate::db::{now_iso, open, write_tx};
use crate::error::{Result, StorageError};
use crate::migrate::{self, Applied};
use crate::schema::KNOWLEDGE_MIGRATIONS;
use crate::V2Layout;

#[derive(Debug, Clone, PartialEq, Default, Serialize)]
pub struct FileRow {
    pub id: String,
    pub rel_path: String,
    pub kind: String,
    pub size: i64,
    pub mtime: f64,
    pub content_hash: Option<String>,
    pub status: String,
    pub parser_version: Option<String>,
    pub chunker_version: Option<String>,
    pub converter_version: Option<String>,
    pub embedding_model_id: Option<String>,
    pub embedding_text_version: Option<String>,
    pub last_error: Option<String>,
    /// Failed processing attempts so far (status `retry`/`failed`).
    pub attempt_count: i64,
    /// When a `retry` file becomes due again.
    pub next_attempt_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EntityRow {
    pub id: String,
    pub file_id: String,
    pub kind: String,
    pub name: String,
    pub qualified_name: String,
    pub language: String,
    pub parent_id: Option<String>,
    pub signature: Option<String>,
    pub start_line: i64,
    pub end_line: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RelationshipRow {
    pub id: String,
    pub file_id: String,
    pub relationship_type: String,
    pub source_entity_id: String,
    pub target_entity_id: Option<String>,
    pub target_symbol: Option<String>,
    pub resolver: String,
    pub confidence: String,
    pub source_location: Option<String>,
    pub evidence: Option<String>,
}

/// EML attachment provenance for a child document.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct AttachmentProvenance {
    pub parent_document_id: String,
    pub name: Option<String>,
    pub content_type: Option<String>,
    pub index: i64,
    pub content_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct DocumentRow {
    pub id: String,
    pub file_id: String,
    pub format: String,
    pub title: Option<String>,
    pub author: Option<String>,
    pub page_count: Option<i64>,
    pub is_scanned: bool,
    pub content_hash: Option<String>,
    pub attachment: Option<AttachmentProvenance>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ChunkRow {
    pub id: String,
    pub document_id: String,
    pub file_id: String,
    pub kind: String,
    pub ordinal: i64,
    pub heading_path: Vec<String>,
    pub heading_level: Option<i64>,
    pub text: String,
    pub search_text: String,
    pub embedding_text: Option<String>,
    pub token_count: Option<i64>,
    pub page_start: Option<i64>,
    pub page_end: Option<i64>,
    pub table_rows: Option<Vec<Vec<String>>>,
    pub caption: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct LinkRow {
    pub id: String,
    pub link_type: String,
    pub entity_id: String,
    pub document_id: String,
    pub chunk_id: Option<String>,
    pub resolver: String,
    pub confidence: String,
    pub evidence: Option<String>,
}

/// Everything extracted from one file, published in one transaction.
#[derive(Debug, Clone, Default)]
pub struct FileKnowledge {
    pub entities: Vec<EntityRow>,
    pub relationships: Vec<RelationshipRow>,
    pub documents: Vec<DocumentRow>,
    pub chunks: Vec<ChunkRow>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Job {
    pub id: String,
    pub build_id: String,
    pub file_id: String,
    pub rel_path: String,
    pub job_type: String,
    pub status: String,
    pub priority: i64,
    pub attempt_count: i64,
    pub next_attempt_at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Hit {
    pub id: String,
    pub rank: f64,
}

/// Quotes each whitespace-separated token so arbitrary user input is a
/// valid FTS5 query of AND-ed terms (no operator injection/syntax errors).
pub fn fts_query(input: &str) -> Option<String> {
    let terms: Vec<String> = input
        .split_whitespace()
        .map(|t| format!("\"{}\"", t.replace('"', "\"\"")))
        .collect();
    (!terms.is_empty()).then(|| terms.join(" "))
}

pub struct ProjectStore {
    conn: Connection,
    source_id: String,
}

impl ProjectStore {
    pub fn open(
        layout: &V2Layout,
        project_id: &str,
        source_id: &str,
        cache_size_mb: i64,
    ) -> Result<(Self, Applied)> {
        let mut conn = open(&layout.project_db(project_id), cache_size_mb)?;
        let applied = migrate::apply(
            &mut conn,
            &format!("knowledge-{project_id}"),
            KNOWLEDGE_MIGRATIONS,
            layout.migration_backups(),
        )?;
        Ok((
            Self {
                conn,
                source_id: source_id.to_owned(),
            },
            applied,
        ))
    }

    pub fn connection(&self) -> &Connection {
        &self.conn
    }

    // ---- builds ----------------------------------------------------

    pub fn create_build(
        &mut self,
        build_id: &str,
        full: bool,
        versions: &IndexVersions,
    ) -> Result<()> {
        let v =
            serde_json::to_string(versions).map_err(|e| StorageError::Invalid(e.to_string()))?;
        self.conn
            .execute(
                "INSERT INTO builds (id, source_id, kind, status, versions, started_at)
                 VALUES (?1, ?2, ?3, 'building', ?4, ?5)",
                params![
                    build_id,
                    self.source_id,
                    if full { "full" } else { "incremental" },
                    v,
                    now_iso()
                ],
            )
            .map_err(StorageError::sqlite("create build"))?;
        Ok(())
    }

    /// Marks `build_id` published and every older published build superseded.
    pub fn mark_published(&mut self, build_id: &str) -> Result<()> {
        let now = now_iso();
        write_tx(&mut self.conn, |tx| {
            tx.execute(
                "UPDATE builds SET status = 'superseded' WHERE status = 'published' AND id <> ?1",
                [build_id],
            )
            .map_err(StorageError::sqlite("supersede builds"))?;
            let n = tx
                .execute(
                    "UPDATE builds SET status = 'published', finished_at = ?2
                     WHERE id = ?1 AND status = 'building'",
                    params![build_id, now],
                )
                .map_err(StorageError::sqlite("publish build"))?;
            if n == 0 {
                return Err(StorageError::Invalid(format!(
                    "build {build_id} is not building"
                )));
            }
            Ok(())
        })
    }

    pub fn mark_aborted(&mut self, build_id: &str) -> Result<()> {
        self.conn
            .execute(
                "UPDATE builds SET status = 'aborted', finished_at = ?2 WHERE id = ?1 AND status = 'building'",
                params![build_id, now_iso()],
            )
            .map_err(StorageError::sqlite("abort build"))?;
        Ok(())
    }

    /// Deletes every row of aborted/superseded builds (never the given
    /// active build). Returns the number of builds removed.
    pub fn gc_builds(&mut self, keep_active: Option<&str>) -> Result<usize> {
        let keep = keep_active.unwrap_or("");
        write_tx(&mut self.conn, |tx| {
            let ids: Vec<String> = {
                let mut stmt = tx
                    .prepare("SELECT id FROM builds WHERE status IN ('aborted', 'superseded') AND id <> ?1")
                    .map_err(StorageError::sqlite("gc builds"))?;
                let rows = stmt
                    .query_map([keep], |r| r.get(0))
                    .map_err(StorageError::sqlite("gc builds"))?;
                rows.collect::<rusqlite::Result<_>>()
                    .map_err(StorageError::sqlite("gc builds"))?
            };
            for id in &ids {
                delete_build_rows(tx, id)?;
                tx.execute("DELETE FROM builds WHERE id = ?1", [id])
                    .map_err(StorageError::sqlite("gc builds"))?;
            }
            Ok(ids.len())
        })
    }

    // ---- files -----------------------------------------------------

    /// File states an indexing pass may compare against. For a full
    /// rebuild this is always empty, so no earlier state can cause a file
    /// to be skipped; for an incremental pass it is the active build's files.
    pub fn reusable_files(&self, plan: &RebuildPlan) -> Result<Vec<FileRow>> {
        match plan {
            RebuildPlan::Full { .. } => Ok(Vec::new()),
            RebuildPlan::Incremental { active_build_id } => self.files(active_build_id),
        }
    }

    pub fn files(&self, build_id: &str) -> Result<Vec<FileRow>> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, rel_path, kind, size, mtime, content_hash, status, parser_version,
                    chunker_version, converter_version, embedding_model_id,
                    embedding_text_version, last_error, attempt_count, next_attempt_at
                 FROM files WHERE build_id = ?1 ORDER BY rel_path",
            )
            .map_err(StorageError::sqlite("list files"))?;
        let rows = stmt
            .query_map([build_id], |r| {
                Ok(FileRow {
                    id: r.get(0)?,
                    rel_path: r.get(1)?,
                    kind: r.get(2)?,
                    size: r.get(3)?,
                    mtime: r.get(4)?,
                    content_hash: r.get(5)?,
                    status: r.get(6)?,
                    parser_version: r.get(7)?,
                    chunker_version: r.get(8)?,
                    converter_version: r.get(9)?,
                    embedding_model_id: r.get(10)?,
                    embedding_text_version: r.get(11)?,
                    last_error: r.get(12)?,
                    attempt_count: r.get(13)?,
                    next_attempt_at: r.get(14)?,
                })
            })
            .map_err(StorageError::sqlite("list files"))?;
        rows.collect::<rusqlite::Result<_>>()
            .map_err(StorageError::sqlite("list files"))
    }

    /// Publishes one file and everything extracted from it into `build_id`,
    /// replacing any earlier rows of that file in the same build.
    pub fn put_file(
        &mut self,
        build_id: &str,
        file: &FileRow,
        knowledge: &FileKnowledge,
    ) -> Result<()> {
        let now = now_iso();
        write_tx(&mut self.conn, |tx| {
            delete_file_rows(tx, build_id, &file.id)?;
            tx.execute(
                "INSERT INTO files (id, source_id, rel_path, kind, size, mtime, content_hash, status,
                    build_id, parser_version, chunker_version, converter_version,
                    embedding_model_id, embedding_text_version, last_indexed_at, last_error,
                    created_at, updated_at, attempt_count, next_attempt_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?15, ?15,
                    ?17, ?18)",
                params![
                    file.id, self.source_id, file.rel_path, file.kind, file.size, file.mtime,
                    file.content_hash, file.status, build_id, file.parser_version,
                    file.chunker_version, file.converter_version, file.embedding_model_id,
                    file.embedding_text_version, now, file.last_error, file.attempt_count,
                    file.next_attempt_at,
                ],
            )
            .map_err(StorageError::sqlite("insert file"))?;
            tx.execute(
                "INSERT INTO path_fts (file_id, build_id, path) VALUES (?1, ?2, ?3)",
                params![file.id, build_id, file.rel_path],
            )
            .map_err(StorageError::sqlite("index path"))?;
            insert_knowledge(tx, build_id, knowledge)
        })
    }

    /// Copies an unchanged file's rows from the active build into a new
    /// incremental build without re-extracting it.
    pub fn carry_forward(&mut self, from_build: &str, to_build: &str, file_id: &str) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            delete_file_rows(tx, to_build, file_id)?;
            let exec = |sql: &str| {
                tx.execute(sql, params![to_build, from_build, file_id])
                    .map_err(StorageError::sqlite("carry forward"))
            };
            exec("INSERT INTO files (id, source_id, rel_path, kind, size, mtime, content_hash, status,
                    build_id, parser_version, chunker_version, converter_version, embedding_model_id,
                    embedding_text_version, last_indexed_at, last_error, created_at, updated_at,
                    attempt_count, next_attempt_at)
                  SELECT id, source_id, rel_path, kind, size, mtime, content_hash, status,
                    ?1, parser_version, chunker_version, converter_version, embedding_model_id,
                    embedding_text_version, last_indexed_at, last_error, created_at, updated_at,
                    attempt_count, next_attempt_at
                  FROM files WHERE build_id = ?2 AND id = ?3")?;
            exec(
                "INSERT INTO path_fts (file_id, build_id, path)
                  SELECT id, ?1, rel_path FROM files WHERE build_id = ?2 AND id = ?3",
            )?;
            exec(
                "INSERT INTO entities SELECT id, ?1, file_id, kind, name, qualified_name, language,
                    parent_id, signature, start_line, end_line, start_col, end_col
                  FROM entities WHERE build_id = ?2 AND file_id = ?3",
            )?;
            exec(
                "INSERT INTO code_fts (entity_id, build_id, name, qualified_name, signature)
                  SELECT id, ?1, name, qualified_name, COALESCE(signature, '')
                  FROM entities WHERE build_id = ?2 AND file_id = ?3",
            )?;
            exec("INSERT INTO relationships SELECT id, ?1, file_id, relationship_type, source_entity_id,
                    target_entity_id, target_symbol, resolver, confidence, source_location, evidence
                  FROM relationships WHERE build_id = ?2 AND file_id = ?3")?;
            exec(
                "INSERT INTO documents SELECT id, ?1, file_id, format, title, author, page_count,
                    is_scanned, content_hash, parent_document_id, attachment_name,
                    attachment_content_type, attachment_index, attachment_content_id
                  FROM documents WHERE build_id = ?2 AND file_id = ?3",
            )?;
            exec("INSERT INTO chunks SELECT id, ?1, document_id, file_id, kind, ordinal, heading_path,
                    heading_level, text, search_text, embedding_text, token_count, page_start,
                    page_end, table_rows, caption
                  FROM chunks WHERE build_id = ?2 AND file_id = ?3")?;
            exec(
                "INSERT INTO chunk_fts (chunk_id, build_id, heading, body, title)
                  SELECT c.id, ?1, f.heading, f.body, f.title
                  FROM chunks c JOIN chunk_fts f ON f.chunk_id = c.id AND f.build_id = c.build_id
                  WHERE c.build_id = ?2 AND c.file_id = ?3",
            )?;
            Ok(())
        })
    }

    /// Copies every file of `from_build` except `exclude` (and all of their
    /// knowledge) into `to_build` in one set-based transaction, applying
    /// `restat` `(file_id, size, mtime)` updates. This is the incremental
    /// fast path: cost is a few statements, not one transaction per file.
    pub fn carry_forward_except(
        &mut self,
        from_build: &str,
        to_build: &str,
        exclude: &[String],
        restat: &[(String, i64, f64)],
    ) -> Result<usize> {
        write_tx(&mut self.conn, |tx| {
            tx.execute_batch(
                "CREATE TEMP TABLE IF NOT EXISTS carry_skip (id TEXT PRIMARY KEY);
                 DELETE FROM carry_skip;",
            )
            .map_err(StorageError::sqlite("prepare carry"))?;
            {
                let mut ins = tx
                    .prepare("INSERT OR IGNORE INTO carry_skip (id) VALUES (?1)")
                    .map_err(StorageError::sqlite("prepare carry"))?;
                for id in exclude {
                    ins.execute([id])
                        .map_err(StorageError::sqlite("prepare carry"))?;
                }
            }
            let exec = |sql: &str| {
                tx.execute(sql, params![to_build, from_build])
                    .map_err(StorageError::sqlite("carry forward"))
            };
            let n = exec(
                "INSERT INTO files (id, source_id, rel_path, kind, size, mtime, content_hash, status,
                    build_id, parser_version, chunker_version, converter_version, embedding_model_id,
                    embedding_text_version, last_indexed_at, last_error, created_at, updated_at,
                    attempt_count, next_attempt_at)
                 SELECT id, source_id, rel_path, kind, size, mtime, content_hash, status,
                    ?1, parser_version, chunker_version, converter_version, embedding_model_id,
                    embedding_text_version, last_indexed_at, last_error, created_at, updated_at,
                    attempt_count, next_attempt_at
                 FROM files WHERE build_id = ?2 AND id NOT IN (SELECT id FROM carry_skip)",
            )?;
            exec(
                "INSERT INTO path_fts (file_id, build_id, path)
                  SELECT id, ?1, rel_path FROM files WHERE build_id = ?2
                    AND id NOT IN (SELECT id FROM carry_skip)",
            )?;
            exec(
                "INSERT INTO entities SELECT id, ?1, file_id, kind, name, qualified_name, language,
                    parent_id, signature, start_line, end_line, start_col, end_col
                  FROM entities WHERE build_id = ?2 AND file_id NOT IN (SELECT id FROM carry_skip)",
            )?;
            exec(
                "INSERT INTO code_fts (entity_id, build_id, name, qualified_name, signature)
                  SELECT id, ?1, name, qualified_name, COALESCE(signature, '')
                  FROM entities WHERE build_id = ?2 AND file_id NOT IN (SELECT id FROM carry_skip)",
            )?;
            exec("INSERT INTO relationships SELECT id, ?1, file_id, relationship_type, source_entity_id,
                    target_entity_id, target_symbol, resolver, confidence, source_location, evidence
                  FROM relationships WHERE build_id = ?2 AND file_id NOT IN (SELECT id FROM carry_skip)")?;
            exec("INSERT INTO documents SELECT id, ?1, file_id, format, title, author, page_count,
                    is_scanned, content_hash, parent_document_id, attachment_name,
                    attachment_content_type, attachment_index, attachment_content_id
                  FROM documents WHERE build_id = ?2 AND file_id NOT IN (SELECT id FROM carry_skip)")?;
            exec("INSERT INTO chunks SELECT id, ?1, document_id, file_id, kind, ordinal, heading_path,
                    heading_level, text, search_text, embedding_text, token_count, page_start,
                    page_end, table_rows, caption
                  FROM chunks WHERE build_id = ?2 AND file_id NOT IN (SELECT id FROM carry_skip)")?;
            exec(
                "INSERT INTO chunk_fts (chunk_id, build_id, heading, body, title)
                  SELECT f.chunk_id, ?1, f.heading, f.body, f.title
                  FROM chunk_fts f JOIN chunks c ON c.id = f.chunk_id AND c.build_id = f.build_id
                  WHERE f.build_id = ?2 AND c.file_id NOT IN (SELECT id FROM carry_skip)",
            )?;
            {
                let mut up = tx
                    .prepare(
                        "UPDATE files SET size = ?1, mtime = ?2 WHERE build_id = ?3 AND id = ?4",
                    )
                    .map_err(StorageError::sqlite("restat"))?;
                for (id, size, mtime) in restat {
                    up.execute(params![size, mtime, to_build, id])
                        .map_err(StorageError::sqlite("restat"))?;
                }
            }
            Ok(n)
        })
    }

    /// Updates the stat of a carried-forward file whose content hash proved
    /// unchanged, so the next run can fast-skip it without re-hashing.
    pub fn set_file_stat(
        &mut self,
        build_id: &str,
        file_id: &str,
        size: i64,
        mtime: f64,
    ) -> Result<()> {
        self.conn
            .execute(
                "UPDATE files SET size = ?1, mtime = ?2 WHERE build_id = ?3 AND id = ?4",
                params![size, mtime, build_id, file_id],
            )
            .map_err(StorageError::sqlite("update file stat"))?;
        Ok(())
    }

    /// Removes a file from a build (deleted/moved files in an incremental build).
    pub fn remove_file(&mut self, build_id: &str, file_id: &str) -> Result<()> {
        write_tx(&mut self.conn, |tx| delete_file_rows(tx, build_id, file_id))
    }

    // ---- links -----------------------------------------------------

    pub fn put_links(&mut self, build_id: &str, links: &[LinkRow]) -> Result<()> {
        write_tx(&mut self.conn, |tx| {
            let mut stmt = tx
                .prepare(
                    "INSERT OR REPLACE INTO cross_links (id, build_id, link_type, entity_id,
                        document_id, chunk_id, resolver, confidence, evidence)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                )
                .map_err(StorageError::sqlite("insert links"))?;
            for l in links {
                stmt.execute(params![
                    l.id,
                    build_id,
                    l.link_type,
                    l.entity_id,
                    l.document_id,
                    l.chunk_id,
                    l.resolver,
                    l.confidence,
                    l.evidence
                ])
                .map_err(StorageError::sqlite("insert link"))?;
            }
            Ok(())
        })
    }

    // ---- reads (always scoped to one build) ---------------------------

    pub fn entities_named(&self, build_id: &str, name: &str) -> Result<Vec<EntityRow>> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, file_id, kind, name, qualified_name, language, parent_id, signature,
                    start_line, end_line
                 FROM entities WHERE build_id = ?1 AND (name = ?2 OR qualified_name = ?2)
                 ORDER BY qualified_name, id",
            )
            .map_err(StorageError::sqlite("entities by name"))?;
        let rows = stmt
            .query_map(params![build_id, name], |r| {
                Ok(EntityRow {
                    id: r.get(0)?,
                    file_id: r.get(1)?,
                    kind: r.get(2)?,
                    name: r.get(3)?,
                    qualified_name: r.get(4)?,
                    language: r.get(5)?,
                    parent_id: r.get(6)?,
                    signature: r.get(7)?,
                    start_line: r.get(8)?,
                    end_line: r.get(9)?,
                })
            })
            .map_err(StorageError::sqlite("entities by name"))?;
        rows.collect::<rusqlite::Result<_>>()
            .map_err(StorageError::sqlite("entities by name"))
    }

    pub fn documents(&self, build_id: &str) -> Result<Vec<DocumentRow>> {
        let mut stmt = self
            .conn
            .prepare(
                "SELECT id, file_id, format, title, author, page_count, is_scanned, content_hash,
                    parent_document_id, attachment_name, attachment_content_type,
                    attachment_index, attachment_content_id
                 FROM documents WHERE build_id = ?1
                 ORDER BY file_id, COALESCE(attachment_index, -1)",
            )
            .map_err(StorageError::sqlite("list documents"))?;
        let rows = stmt
            .query_map([build_id], |r| {
                let parent: Option<String> = r.get(8)?;
                let index: Option<i64> = r.get(11)?;
                Ok(DocumentRow {
                    id: r.get(0)?,
                    file_id: r.get(1)?,
                    format: r.get(2)?,
                    title: r.get(3)?,
                    author: r.get(4)?,
                    page_count: r.get(5)?,
                    is_scanned: r.get::<_, i64>(6)? != 0,
                    content_hash: r.get(7)?,
                    attachment: match (parent, index) {
                        (Some(parent_document_id), Some(index)) => Some(AttachmentProvenance {
                            parent_document_id,
                            name: r.get(9)?,
                            content_type: r.get(10)?,
                            index,
                            content_id: r.get(12)?,
                        }),
                        _ => None,
                    },
                })
            })
            .map_err(StorageError::sqlite("list documents"))?;
        rows.collect::<rusqlite::Result<_>>()
            .map_err(StorageError::sqlite("list documents"))
    }

    fn fts(&self, sql: &str, build_id: &str, query: &str, limit: i64) -> Result<Vec<Hit>> {
        let Some(q) = fts_query(query) else {
            return Ok(Vec::new());
        };
        let mut stmt = self
            .conn
            .prepare(sql)
            .map_err(StorageError::sqlite("fts search"))?;
        let rows = stmt
            .query_map(params![q, build_id, limit], |r| {
                Ok(Hit {
                    id: r.get(0)?,
                    rank: r.get(1)?,
                })
            })
            .map_err(StorageError::sqlite("fts search"))?;
        rows.collect::<rusqlite::Result<_>>()
            .map_err(StorageError::sqlite("fts search"))
    }

    /// BM25-ranked chunk ids (best first) within one build.
    pub fn search_chunks(&self, build_id: &str, query: &str, limit: i64) -> Result<Vec<Hit>> {
        self.fts(
            "SELECT chunk_id, bm25(chunk_fts) FROM chunk_fts
             WHERE chunk_fts MATCH ?1 AND build_id = ?2 ORDER BY bm25(chunk_fts), chunk_id LIMIT ?3",
            build_id,
            query,
            limit,
        )
    }

    pub fn search_code(&self, build_id: &str, query: &str, limit: i64) -> Result<Vec<Hit>> {
        self.fts(
            "SELECT entity_id, bm25(code_fts) FROM code_fts
             WHERE code_fts MATCH ?1 AND build_id = ?2 ORDER BY bm25(code_fts), entity_id LIMIT ?3",
            build_id,
            query,
            limit,
        )
    }

    pub fn search_paths(&self, build_id: &str, query: &str, limit: i64) -> Result<Vec<Hit>> {
        self.fts(
            "SELECT file_id, bm25(path_fts) FROM path_fts
             WHERE path_fts MATCH ?1 AND build_id = ?2 ORDER BY bm25(path_fts), file_id LIMIT ?3",
            build_id,
            query,
            limit,
        )
    }

    pub fn count(&self, table: &str, build_id: &str) -> Result<i64> {
        const ALLOWED: &[&str] = &[
            "files",
            "entities",
            "relationships",
            "documents",
            "chunks",
            "cross_links",
        ];
        if !ALLOWED.contains(&table) {
            return Err(StorageError::Invalid(format!("cannot count {table}")));
        }
        self.conn
            .query_row(
                &format!("SELECT COUNT(*) FROM {table} WHERE build_id = ?1"),
                [build_id],
                |r| r.get(0),
            )
            .map_err(StorageError::sqlite("count"))
    }

    // ---- jobs & errors -----------------------------------------------

    pub fn enqueue(
        &mut self,
        build_id: &str,
        file_id: &str,
        rel_path: &str,
        priority: i64,
    ) -> Result<String> {
        let id = ragmonk_core::ids::v2::build_id(build_id, &format!("job\x1f{file_id}"));
        self.conn
            .execute(
                "INSERT OR IGNORE INTO index_jobs (id, build_id, file_id, rel_path, job_type, status,
                    priority, created_at)
                 VALUES (?1, ?2, ?3, ?4, 'index_file', 'queued', ?5, ?6)",
                params![id, build_id, file_id, rel_path, priority, now_iso()],
            )
            .map_err(StorageError::sqlite("enqueue"))?;
        Ok(id)
    }

    /// Claims the next due job (priority, then age) and marks it processing.
    pub fn claim_next(&mut self, build_id: &str) -> Result<Option<Job>> {
        let now = now_iso();
        write_tx(&mut self.conn, |tx| {
            let job = tx
                .query_row(
                    "SELECT id, build_id, file_id, rel_path, job_type, status, priority,
                        attempt_count, next_attempt_at
                     FROM index_jobs
                     WHERE build_id = ?1 AND status IN ('queued', 'retry')
                       AND (next_attempt_at IS NULL OR next_attempt_at <= ?2)
                     ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                    params![build_id, now],
                    |r| {
                        Ok(Job {
                            id: r.get(0)?,
                            build_id: r.get(1)?,
                            file_id: r.get(2)?,
                            rel_path: r.get(3)?,
                            job_type: r.get(4)?,
                            status: "processing".into(),
                            priority: r.get(6)?,
                            attempt_count: r.get(7)?,
                            next_attempt_at: r.get(8)?,
                        })
                    },
                )
                .optional()
                .map_err(StorageError::sqlite("claim job"))?;
            if let Some(job) = &job {
                tx.execute(
                    "UPDATE index_jobs SET status = 'processing', started_at = ?1 WHERE id = ?2",
                    params![now, job.id],
                )
                .map_err(StorageError::sqlite("claim job"))?;
            }
            Ok(job)
        })
    }

    pub fn complete_job(&mut self, job_id: &str) -> Result<()> {
        self.conn
            .execute(
                "UPDATE index_jobs SET status = 'completed', completed_at = ?1 WHERE id = ?2",
                params![now_iso(), job_id],
            )
            .map_err(StorageError::sqlite("complete job"))?;
        Ok(())
    }

    /// `permanent` = failed; otherwise retry at `next_attempt_at`.
    pub fn fail_job(
        &mut self,
        job_id: &str,
        error_code: &str,
        error_message: &str,
        next_attempt_at: Option<&str>,
        permanent: bool,
    ) -> Result<()> {
        self.conn
            .execute(
                "UPDATE index_jobs SET status = ?1, attempt_count = attempt_count + 1,
                    next_attempt_at = ?2, error_code = ?3, error_message = ?4,
                    completed_at = ?5 WHERE id = ?6",
                params![
                    if permanent { "failed" } else { "retry" },
                    next_attempt_at,
                    error_code,
                    error_message,
                    permanent.then(now_iso),
                    job_id
                ],
            )
            .map_err(StorageError::sqlite("fail job"))?;
        Ok(())
    }

    /// Crash recovery: jobs left `processing` go back to `queued`.
    pub fn recover_stuck(&mut self) -> Result<usize> {
        self.conn
            .execute(
                "UPDATE index_jobs SET status = 'queued', started_at = NULL WHERE status = 'processing'",
                [],
            )
            .map_err(StorageError::sqlite("recover stuck jobs"))
    }

    pub fn queue_depth(&self, build_id: &str) -> Result<i64> {
        self.conn
            .query_row(
                "SELECT COUNT(*) FROM index_jobs WHERE build_id = ?1 AND status IN ('queued', 'retry', 'processing')",
                [build_id],
                |r| r.get(0),
            )
            .map_err(StorageError::sqlite("queue depth"))
    }

    pub fn record_error(
        &mut self,
        build_id: Option<&str>,
        file_id: Option<&str>,
        rel_path: Option<&str>,
        code: &str,
        message: &str,
    ) -> Result<()> {
        let now = now_iso();
        let id = ragmonk_core::ids::v2::build_id(
            &self.source_id,
            &format!("error\x1f{now}\x1f{code}\x1f{}", rel_path.unwrap_or("")),
        );
        self.conn
            .execute(
                "INSERT OR REPLACE INTO index_errors (id, source_id, build_id, file_id, rel_path,
                    error_code, error_message, occurred_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![
                    id,
                    self.source_id,
                    build_id,
                    file_id,
                    rel_path,
                    code,
                    message,
                    now
                ],
            )
            .map_err(StorageError::sqlite("record error"))?;
        Ok(())
    }
}

fn delete_build_rows(tx: &Transaction<'_>, build_id: &str) -> Result<()> {
    for sql in [
        "DELETE FROM code_fts WHERE build_id = ?1",
        "DELETE FROM chunk_fts WHERE build_id = ?1",
        "DELETE FROM path_fts WHERE build_id = ?1",
        "DELETE FROM cross_links WHERE build_id = ?1",
        "DELETE FROM chunks WHERE build_id = ?1",
        "DELETE FROM documents WHERE build_id = ?1",
        "DELETE FROM relationships WHERE build_id = ?1",
        "DELETE FROM entities WHERE build_id = ?1",
        "DELETE FROM index_jobs WHERE build_id = ?1",
        "DELETE FROM files WHERE build_id = ?1",
    ] {
        tx.execute(sql, [build_id])
            .map_err(StorageError::sqlite("delete build rows"))?;
    }
    Ok(())
}

fn delete_file_rows(tx: &Transaction<'_>, build_id: &str, file_id: &str) -> Result<()> {
    for sql in [
        "DELETE FROM code_fts WHERE build_id = ?1 AND entity_id IN
            (SELECT id FROM entities WHERE build_id = ?1 AND file_id = ?2)",
        "DELETE FROM chunk_fts WHERE build_id = ?1 AND chunk_id IN
            (SELECT id FROM chunks WHERE build_id = ?1 AND file_id = ?2)",
        "DELETE FROM path_fts WHERE build_id = ?1 AND file_id = ?2",
        "DELETE FROM cross_links WHERE build_id = ?1 AND document_id IN
            (SELECT id FROM documents WHERE build_id = ?1 AND file_id = ?2)",
        "DELETE FROM cross_links WHERE build_id = ?1 AND entity_id IN
            (SELECT id FROM entities WHERE build_id = ?1 AND file_id = ?2)",
        "DELETE FROM chunks WHERE build_id = ?1 AND file_id = ?2",
        "DELETE FROM documents WHERE build_id = ?1 AND file_id = ?2",
        "DELETE FROM relationships WHERE build_id = ?1 AND file_id = ?2",
        "DELETE FROM entities WHERE build_id = ?1 AND file_id = ?2",
        "DELETE FROM files WHERE build_id = ?1 AND id = ?2",
    ] {
        tx.execute(sql, params![build_id, file_id])
            .map_err(StorageError::sqlite("delete file rows"))?;
    }
    Ok(())
}

fn insert_knowledge(tx: &Transaction<'_>, build_id: &str, k: &FileKnowledge) -> Result<()> {
    let err = |what: &'static str| StorageError::sqlite(what);
    for e in &k.entities {
        tx.execute(
            "INSERT INTO entities (id, build_id, file_id, kind, name, qualified_name, language,
                parent_id, signature, start_line, end_line)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                e.id,
                build_id,
                e.file_id,
                e.kind,
                e.name,
                e.qualified_name,
                e.language,
                e.parent_id,
                e.signature,
                e.start_line,
                e.end_line
            ],
        )
        .map_err(err("insert entity"))?;
        tx.execute(
            "INSERT INTO code_fts (entity_id, build_id, name, qualified_name, signature)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                e.id,
                build_id,
                e.name,
                e.qualified_name,
                e.signature.clone().unwrap_or_default()
            ],
        )
        .map_err(err("index entity"))?;
    }
    for r in &k.relationships {
        tx.execute(
            "INSERT INTO relationships (id, build_id, file_id, relationship_type, source_entity_id,
                target_entity_id, target_symbol, resolver, confidence, source_location, evidence)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                r.id,
                build_id,
                r.file_id,
                r.relationship_type,
                r.source_entity_id,
                r.target_entity_id,
                r.target_symbol,
                r.resolver,
                r.confidence,
                r.source_location,
                r.evidence
            ],
        )
        .map_err(err("insert relationship"))?;
    }
    for d in &k.documents {
        let a = d.attachment.as_ref();
        tx.execute(
            "INSERT INTO documents (id, build_id, file_id, format, title, author, page_count,
                is_scanned, content_hash, parent_document_id, attachment_name,
                attachment_content_type, attachment_index, attachment_content_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
            params![
                d.id,
                build_id,
                d.file_id,
                d.format,
                d.title,
                d.author,
                d.page_count,
                i64::from(d.is_scanned),
                d.content_hash,
                a.map(|a| a.parent_document_id.clone()),
                a.and_then(|a| a.name.clone()),
                a.and_then(|a| a.content_type.clone()),
                a.map(|a| a.index),
                a.and_then(|a| a.content_id.clone())
            ],
        )
        .map_err(err("insert document"))?;
    }
    for c in &k.chunks {
        let heading_path = serde_json::to_string(&c.heading_path).unwrap_or_else(|_| "[]".into());
        let rows = c
            .table_rows
            .as_ref()
            .map(|r| serde_json::to_string(r).unwrap_or_else(|_| "[]".into()));
        tx.execute(
            "INSERT INTO chunks (id, build_id, document_id, file_id, kind, ordinal, heading_path,
                heading_level, text, search_text, embedding_text, token_count, page_start,
                page_end, table_rows, caption)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
            params![
                c.id,
                build_id,
                c.document_id,
                c.file_id,
                c.kind,
                c.ordinal,
                heading_path,
                c.heading_level,
                c.text,
                c.search_text,
                c.embedding_text,
                c.token_count,
                c.page_start,
                c.page_end,
                rows,
                c.caption
            ],
        )
        .map_err(err("insert chunk"))?;
        let title: Option<String> = k
            .documents
            .iter()
            .find(|d| d.id == c.document_id)
            .and_then(|d| d.title.clone());
        tx.execute(
            "INSERT INTO chunk_fts (chunk_id, build_id, heading, body, title) VALUES (?1, ?2, ?3, ?4, ?5)",
            params![c.id, build_id, c.heading_path.join(" > "), c.search_text, title.unwrap_or_default()],
        )
        .map_err(err("index chunk"))?;
    }
    Ok(())
}
