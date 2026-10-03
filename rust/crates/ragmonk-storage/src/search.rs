//! Lexical search projections over one build (RUST-10). Each query returns
//! rows with their file already joined in, mirroring the reference's
//! `entities_repo`/`documents_repo`/`files_repo` search projections.
//! Ranking and tiering live in `ragmonk-retrieval`.

use rusqlite::params;

use crate::error::{Result, StorageError};
use crate::knowledge::ProjectStore;

/// Document-FTS column weights (`bm25(chunk_fts, ...)`): chunk id and build
/// id are unindexed placeholders, then heading 5, body 1, title 8 (the
/// reference's `_DOCUMENT_FTS_COLUMN_WEIGHTS`).
const CHUNK_BM25: &str = "bm25(chunk_fts, 0.0, 0.0, 5.0, 1.0, 8.0)";

#[derive(Debug, Clone, PartialEq)]
pub struct EntitySearchRow {
    pub id: String,
    pub name: String,
    pub qualified_name: String,
    pub kind: String,
    pub signature: Option<String>,
    pub start_line: i64,
    pub end_line: i64,
    pub rel_path: String,
    pub mtime: f64,
    /// Position in the FTS result (0 for non-FTS lookups).
    pub fts_rank: u32,
    pub bm25: Option<f64>,
}

/// Provenance of a hit inside an email attachment.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct AttachmentProvenance {
    pub name: Option<String>,
    pub content_type: Option<String>,
    pub index: Option<i64>,
    pub format: String,
    pub parent_document_id: String,
    pub parent_title: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DocumentSearchRow {
    /// Chunk id for FTS hits, document id for title hits.
    pub id: String,
    pub title: String,
    pub rel_path: String,
    pub mtime: f64,
    pub snippet: Option<String>,
    pub heading: Option<String>,
    pub heading_path: Vec<String>,
    pub page_start: Option<i64>,
    pub page_end: Option<i64>,
    pub attachment: Option<AttachmentProvenance>,
    pub fts_rank: u32,
    pub bm25: Option<f64>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PathSearchRow {
    pub id: String,
    pub rel_path: String,
    pub mtime: f64,
}

const ENTITY_PROJECTION: &str = "SELECT e.id, e.name, e.qualified_name, e.kind, e.signature,
        e.start_line, e.end_line, f.rel_path, f.mtime
     FROM entities e JOIN files f ON f.build_id = e.build_id AND f.id = e.file_id";

fn entity_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<EntitySearchRow> {
    Ok(EntitySearchRow {
        id: r.get(0)?,
        name: r.get(1)?,
        qualified_name: r.get(2)?,
        kind: r.get(3)?,
        signature: r.get(4)?,
        start_line: r.get(5)?,
        end_line: r.get(6)?,
        rel_path: r.get(7)?,
        mtime: r.get(8)?,
        fts_rank: 0,
        bm25: None,
    })
}

const ATTACHMENT_COLUMNS: &str = "d.parent_document_id, d.attachment_name,
    d.attachment_content_type, d.attachment_index, d.format, p.title";

/// Reads the six [`ATTACHMENT_COLUMNS`] starting at `at`.
fn attachment(r: &rusqlite::Row<'_>, at: usize) -> rusqlite::Result<Option<AttachmentProvenance>> {
    let parent: Option<String> = r.get(at)?;
    Ok(match parent {
        None => None,
        Some(parent_document_id) => Some(AttachmentProvenance {
            name: r.get(at + 1)?,
            content_type: r.get(at + 2)?,
            index: r.get(at + 3)?,
            format: r.get(at + 4)?,
            parent_document_id,
            parent_title: r.get(at + 5)?,
        }),
    })
}

/// SQL `LIKE` pattern matching `s` literally (escape `\`).
fn like_literal(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

impl ProjectStore {
    /// Entities whose name or qualified name equals `q`.
    pub fn search_entities_exact(&self, build_id: &str, q: &str) -> Result<Vec<EntitySearchRow>> {
        self.query_rows(
            "entity exact search",
            &format!(
                "{ENTITY_PROJECTION} WHERE e.build_id = ?1 AND (e.name = ?2 OR e.qualified_name = ?2)
                 ORDER BY e.qualified_name, e.file_id, e.start_line"
            ),
            &[&build_id, &q],
            entity_row,
        )
    }

    /// Entities whose alias equals `alias`. The alias is the last two
    /// dot-separated segments of a qualified name with at least three
    /// segments (the reference's `compute_alias`), so only a query with
    /// exactly one dot can match.
    pub fn search_entities_alias(
        &self,
        build_id: &str,
        alias: &str,
        limit: i64,
    ) -> Result<Vec<EntitySearchRow>> {
        if alias.matches('.').count() != 1 {
            return Ok(Vec::new());
        }
        let pattern = format!("%.{}", like_literal(alias));
        self.query_rows(
            "entity alias search",
            &format!(
                "{ENTITY_PROJECTION} WHERE e.build_id = ?1
                   AND e.qualified_name LIKE ?2 ESCAPE '\\'
                   AND length(e.qualified_name) - length(replace(e.qualified_name, '.', '')) >= 2
                 ORDER BY e.qualified_name, e.file_id, e.start_line LIMIT ?3"
            ),
            &[&build_id, &pattern, &limit],
            entity_row,
        )
    }

    /// BM25-ranked entities for an FTS5 `expression` (already sanitized).
    pub fn search_entities_fts(
        &self,
        build_id: &str,
        expression: &str,
        limit: i64,
    ) -> Result<Vec<EntitySearchRow>> {
        let mut rows = self.query_rows(
            "entity fts search",
            "SELECT e.id, e.name, e.qualified_name, e.kind, e.signature,
                    e.start_line, e.end_line, f.rel_path, f.mtime, bm25(code_fts)
             FROM code_fts
             JOIN entities e ON e.rowid = code_fts.rowid
             JOIN files f ON f.build_id = e.build_id AND f.id = e.file_id
             WHERE code_fts MATCH ?1 AND code_fts.build_id = ?2
             ORDER BY bm25(code_fts) LIMIT ?3",
            &[&expression, &build_id, &limit],
            |r| {
                let mut row = entity_row(r)?;
                row.bm25 = Some(r.get(9)?);
                Ok(row)
            },
        )?;
        for (i, row) in rows.iter_mut().enumerate() {
            row.fts_rank = i as u32;
        }
        Ok(rows)
    }

    /// Documents whose title equals `title`, case-insensitively.
    pub fn search_document_titles(
        &self,
        build_id: &str,
        title: &str,
        limit: i64,
    ) -> Result<Vec<DocumentSearchRow>> {
        self.query_rows(
            "document title search",
            &format!(
                "SELECT d.id, d.title, f.rel_path, f.mtime, {ATTACHMENT_COLUMNS}
                 FROM documents d
                 JOIN files f ON f.build_id = d.build_id AND f.id = d.file_id
                 LEFT JOIN documents p ON p.build_id = d.build_id AND p.id = d.parent_document_id
                 WHERE d.build_id = ?1 AND d.title = ?2 COLLATE NOCASE
                 LIMIT ?3"
            ),
            &[&build_id, &title, &limit],
            |r| {
                let title: String = r.get(1)?;
                Ok(DocumentSearchRow {
                    id: r.get(0)?,
                    snippet: Some(title.clone()),
                    title,
                    rel_path: r.get(2)?,
                    mtime: r.get(3)?,
                    heading: None,
                    heading_path: Vec::new(),
                    page_start: None,
                    page_end: None,
                    attachment: attachment(r, 4)?,
                    fts_rank: 0,
                    bm25: None,
                })
            },
        )
    }

    /// BM25-ranked chunks (heading 5, body 1, title 8) for an FTS5
    /// `expression`. Each carries a match-centred snippet of at most
    /// `snippet_tokens` tokens (clamped to 1..=64).
    pub fn search_chunks_fts(
        &self,
        build_id: &str,
        expression: &str,
        limit: i64,
        snippet_tokens: i64,
    ) -> Result<Vec<DocumentSearchRow>> {
        let tokens = snippet_tokens.clamp(1, 64);
        let mut stmt = self
            .conn
            .prepare_cached(&format!(
                "SELECT c.id, fts.heading, fts.body, fts.title, d.title, f.rel_path, f.mtime,
                        c.page_start, c.page_end, c.heading_path, {ATTACHMENT_COLUMNS},
                        {CHUNK_BM25}, snippet(chunk_fts, -1, '', '', '...', ?1)
                 FROM chunk_fts fts
                 JOIN chunks c ON c.rowid = fts.rowid
                 JOIN documents d ON d.build_id = c.build_id AND d.id = c.document_id
                 JOIN files f ON f.build_id = c.build_id AND f.id = c.file_id
                 LEFT JOIN documents p ON p.build_id = d.build_id AND p.id = d.parent_document_id
                 WHERE chunk_fts MATCH ?2 AND fts.build_id = ?3
                 ORDER BY {CHUNK_BM25} LIMIT ?4"
            ))
            .map_err(StorageError::sqlite("chunk fts search"))?;
        let rows = stmt
            .query_map(params![tokens, expression, build_id, limit], |r| {
                let heading: String = r.get(1)?;
                let body: String = r.get(2)?;
                let fts_title: String = r.get(3)?;
                let doc_title: Option<String> = r.get(4)?;
                let rel_path: String = r.get(5)?;
                let heading_path: String = r.get(9)?;
                let snippet: Option<String> = r.get(17)?;
                let snippet = snippet
                    .map(|s| s.trim().to_owned())
                    .filter(|s| !s.is_empty());
                let fallback: String = if body.is_empty() { &heading } else { &body }
                    .chars()
                    .take(crate::vectors::SNIPPET_CHARS)
                    .collect();
                let title = [Some(fts_title), doc_title]
                    .into_iter()
                    .flatten()
                    .find(|t| !t.is_empty())
                    .unwrap_or_else(|| rel_path.clone());
                Ok(DocumentSearchRow {
                    id: r.get(0)?,
                    title,
                    rel_path,
                    mtime: r.get(6)?,
                    snippet: snippet.or((!fallback.is_empty()).then_some(fallback)),
                    heading: (!heading.is_empty()).then_some(heading),
                    heading_path: serde_json::from_str(&heading_path).unwrap_or_default(),
                    page_start: r.get(7)?,
                    page_end: r.get(8)?,
                    attachment: attachment(r, 10)?,
                    fts_rank: 0,
                    bm25: Some(r.get(16)?),
                })
            })
            .map_err(StorageError::sqlite("chunk fts search"))?;
        let mut out: Vec<DocumentSearchRow> = rows
            .collect::<rusqlite::Result<_>>()
            .map_err(StorageError::sqlite("chunk fts search"))?;
        for (i, row) in out.iter_mut().enumerate() {
            row.fts_rank = i as u32;
        }
        Ok(out)
    }

    /// Files whose path matches every token of `q` (FTS, BM25 then path
    /// order), falling back to a substring scan when that finds nothing
    /// (e.g. a mid-word fragment).
    pub fn search_paths_projection(
        &self,
        build_id: &str,
        q: &str,
        limit: i64,
    ) -> Result<Vec<PathSearchRow>> {
        let map = |r: &rusqlite::Row<'_>| -> rusqlite::Result<PathSearchRow> {
            Ok(PathSearchRow {
                id: r.get(0)?,
                rel_path: r.get(1)?,
                mtime: r.get(2)?,
            })
        };
        let tokens = word_tokens(q);
        if !tokens.is_empty() {
            let expression = tokens
                .iter()
                .map(|t| format!("\"{t}\""))
                .collect::<Vec<_>>()
                .join(" AND ");
            let rows = self.query_rows(
                "path fts search",
                "SELECT f.id, f.rel_path, f.mtime FROM path_fts
                 JOIN files f ON f.rowid = path_fts.rowid
                 WHERE path_fts MATCH ?1 AND path_fts.build_id = ?2
                 ORDER BY bm25(path_fts), f.rel_path LIMIT ?3",
                &[&expression, &build_id, &limit],
                map,
            )?;
            if !rows.is_empty() {
                return Ok(rows);
            }
        }
        let pattern = format!("%{}%", like_literal(q));
        self.query_rows(
            "path substring search",
            "SELECT id, rel_path, mtime FROM files
             WHERE build_id = ?1 AND rel_path LIKE ?2 ESCAPE '\\'
             ORDER BY rel_path LIMIT ?3",
            &[&build_id, &pattern, &limit],
            map,
        )
    }
}

/// Word tokens (`\w+`, Unicode-aware), the reference's `re.findall(r"\w+")`.
pub fn word_tokens(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for c in text.chars() {
        if c.is_alphanumeric() || c == '_' || is_mark(c) {
            cur.push(c);
        } else if !cur.is_empty() {
            out.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// Combining marks count as word characters in Python's `\w`.
fn is_mark(c: char) -> bool {
    matches!(c as u32, 0x0300..=0x036F | 0x1AB0..=0x1AFF | 0x1DC0..=0x1DFF | 0x20D0..=0x20FF | 0xFE20..=0xFE2F)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn word_tokens_match_python_w() {
        assert_eq!(
            word_tokens("settlement.BetSettled"),
            ["settlement", "BetSettled"]
        );
        assert_eq!(word_tokens("my-helpers.v2"), ["my", "helpers", "v2"]);
        assert_eq!(
            word_tokens("café_total Λογαριασμός"),
            ["café_total", "Λογαριασμός"]
        );
        assert!(word_tokens("(( )) $$$").is_empty());
        assert_eq!(
            word_tokens("日本語のドキュメント"),
            ["日本語のドキュメント"]
        );
    }

    #[test]
    fn like_literal_escapes_wildcards() {
        assert_eq!(like_literal("a_b%c\\d"), "a\\_b\\%c\\\\d");
    }
}
