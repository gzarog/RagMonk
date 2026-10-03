//! V2 DDL. Clean-slate design (V3 plan): no V1 column compatibility.
//!
//! Every index-derived row carries the `build_id` that produced it. Reads
//! must join on the source's `active_build_id` so an in-progress (or
//! aborted) build is never visible — the local equivalent of server-side
//! atomic publication.

use crate::migrate::Migration;

/// Bumped with every V2 schema change that requires a full rebuild.
pub const V2_SCHEMA_VERSION: i64 = 1;

pub const CONTROL_MIGRATIONS: &[Migration] = &[Migration {
    version: 1,
    name: "v2_control_plane",
    sql: r#"
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- User-owned source definitions (the only thing imported from V1).
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ('local', 'network')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    include_patterns TEXT NOT NULL DEFAULT '[]',
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    origin TEXT NOT NULL CHECK (origin IN ('v2', 'v1_import')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Index-derived, V2-only state. A source without a published V2 build is
-- always 'needs_full_rebuild'.
CREATE TABLE source_state (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    build_state TEXT NOT NULL
        CHECK (build_state IN ('needs_full_rebuild', 'building', 'ready', 'failed')),
    rebuild_reason TEXT,
    online_status TEXT NOT NULL DEFAULT 'active' CHECK (online_status IN ('active', 'offline')),
    active_build_id TEXT,
    pending_build_id TEXT,
    versions TEXT,
    last_full_build_at TEXT,
    last_scan_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- Audit trail of migrations/imports run against this home.
CREATE TABLE migration_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"#,
}];

pub const KNOWLEDGE_MIGRATIONS: &[Migration] = &[
    Migration {
        version: 1,
        name: "v2_knowledge",
        sql: r#"
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE builds (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('full', 'incremental')),
    status TEXT NOT NULL CHECK (status IN ('building', 'published', 'aborted', 'superseded')),
    versions TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE files (
    id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('code', 'document', 'unknown')),
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    content_hash TEXT,
    status TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    parser_version TEXT,
    chunker_version TEXT,
    converter_version TEXT,
    embedding_model_id TEXT,
    embedding_text_version TEXT,
    last_indexed_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (build_id, id),
    UNIQUE (build_id, rel_path)
);
CREATE INDEX idx_files_build ON files(build_id);
CREATE INDEX idx_files_hash ON files(content_hash);

CREATE TABLE index_jobs (
    id TEXT PRIMARY KEY,
    build_id TEXT NOT NULL REFERENCES builds(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'processing', 'retry', 'failed', 'completed')),
    priority INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT
);
CREATE INDEX idx_jobs_status ON index_jobs(status, next_attempt_at);

CREATE TABLE index_errors (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    build_id TEXT,
    file_id TEXT,
    rel_path TEXT,
    error_code TEXT NOT NULL,
    error_message TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX idx_errors_time ON index_errors(occurred_at);

CREATE TABLE entities (
    id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    file_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    language TEXT NOT NULL,
    parent_id TEXT,
    signature TEXT,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER NOT NULL DEFAULT 0,
    end_col INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_id, id)
);
CREATE INDEX idx_entities_name ON entities(build_id, name);
CREATE INDEX idx_entities_qname ON entities(build_id, qualified_name);
CREATE INDEX idx_entities_file ON entities(build_id, file_id);

CREATE TABLE relationships (
    id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    file_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT,
    target_symbol TEXT,
    resolver TEXT NOT NULL,
    confidence TEXT NOT NULL,
    source_location TEXT,
    evidence TEXT,
    PRIMARY KEY (build_id, id)
);
CREATE INDEX idx_rel_source ON relationships(build_id, source_entity_id);
CREATE INDEX idx_rel_target ON relationships(build_id, target_entity_id);
CREATE INDEX idx_rel_file ON relationships(build_id, file_id);

-- Top-level documents and EML attachment child documents.
CREATE TABLE documents (
    id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    file_id TEXT NOT NULL,
    format TEXT NOT NULL,
    title TEXT,
    author TEXT,
    page_count INTEGER,
    is_scanned INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT,
    parent_document_id TEXT,
    attachment_name TEXT,
    attachment_content_type TEXT,
    attachment_index INTEGER,
    attachment_content_id TEXT,
    PRIMARY KEY (build_id, id),
    CHECK ((parent_document_id IS NULL) = (attachment_index IS NULL))
);
CREATE INDEX idx_documents_file ON documents(build_id, file_id);
CREATE INDEX idx_documents_parent ON documents(build_id, parent_document_id);

-- Retrieval chunks (heading / paragraph / table).
CREATE TABLE chunks (
    id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    document_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('heading', 'paragraph', 'table')),
    ordinal INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '[]',
    heading_level INTEGER,
    text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    embedding_text TEXT,
    token_count INTEGER,
    page_start INTEGER,
    page_end INTEGER,
    table_rows TEXT,
    caption TEXT,
    PRIMARY KEY (build_id, id)
);
CREATE INDEX idx_chunks_document ON chunks(build_id, document_id, ordinal);
CREATE INDEX idx_chunks_file ON chunks(build_id, file_id);

CREATE TABLE cross_links (
    id TEXT NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(id),
    link_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    chunk_id TEXT,
    resolver TEXT NOT NULL,
    confidence TEXT NOT NULL,
    evidence TEXT,
    PRIMARY KEY (build_id, id)
);
CREATE INDEX idx_links_entity ON cross_links(build_id, entity_id);
CREATE INDEX idx_links_document ON cross_links(build_id, document_id);

-- Manual links are user data, not index-derived: they survive rebuilds.
CREATE TABLE manual_links (
    id TEXT PRIMARY KEY,
    link_type TEXT NOT NULL,
    entity_qualified_name TEXT NOT NULL,
    document_rel_path TEXT NOT NULL,
    attachment_index INTEGER,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (link_type, entity_qualified_name, document_rel_path, attachment_index)
);

-- Lexical indexes. Rows are written explicitly together with their base
-- rows and carry build_id so searches filter to the active build.
CREATE VIRTUAL TABLE code_fts USING fts5(
    entity_id UNINDEXED, build_id UNINDEXED, name, qualified_name, signature
);
CREATE VIRTUAL TABLE chunk_fts USING fts5(
    chunk_id UNINDEXED, build_id UNINDEXED, heading, body, title
);
CREATE VIRTUAL TABLE path_fts USING fts5(
    file_id UNINDEXED, build_id UNINDEXED, path
);
"#,
    },
    Migration {
        version: 2,
        name: "file_retry_state",
        sql: r#"
-- Durable per-file retry state (RUST-04): a transiently failing file is
-- retried on a later run once next_attempt_at is due, like the reference's
-- job backoff, without blocking publication of the rest of the source.
ALTER TABLE files ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE files ADD COLUMN next_attempt_at TEXT;
"#,
    },
    Migration {
        version: 3,
        name: "code_references",
        sql: r#"
-- Code intelligence (RUST-05): the raw reference text a relationship was
-- extracted from, so cross-file targets can be re-resolved against the
-- whole build before publication instead of depending on file order.
ALTER TABLE relationships ADD COLUMN reference_text TEXT;
CREATE INDEX idx_rel_resolver ON relationships(build_id, resolver);
CREATE INDEX idx_rel_symbol ON relationships(build_id, target_symbol);

-- Each lexical row now shares its base row's rowid, so per-file and
-- per-build deletes are rowid lookups instead of full FTS scans (which made
-- indexing quadratic). Existing rows are re-keyed with their content intact.
CREATE VIRTUAL TABLE code_fts_v3 USING fts5(
    entity_id UNINDEXED, build_id UNINDEXED, name, qualified_name, signature
);
INSERT INTO code_fts_v3 (rowid, entity_id, build_id, name, qualified_name, signature)
    SELECT e.rowid, f.entity_id, f.build_id, f.name, f.qualified_name, f.signature
    FROM code_fts f JOIN entities e ON e.id = f.entity_id AND e.build_id = f.build_id;
DROP TABLE code_fts;
ALTER TABLE code_fts_v3 RENAME TO code_fts;

CREATE VIRTUAL TABLE chunk_fts_v3 USING fts5(
    chunk_id UNINDEXED, build_id UNINDEXED, heading, body, title
);
INSERT INTO chunk_fts_v3 (rowid, chunk_id, build_id, heading, body, title)
    SELECT c.rowid, f.chunk_id, f.build_id, f.heading, f.body, f.title
    FROM chunk_fts f JOIN chunks c ON c.id = f.chunk_id AND c.build_id = f.build_id;
DROP TABLE chunk_fts;
ALTER TABLE chunk_fts_v3 RENAME TO chunk_fts;

CREATE VIRTUAL TABLE path_fts_v3 USING fts5(
    file_id UNINDEXED, build_id UNINDEXED, path
);
INSERT INTO path_fts_v3 (rowid, file_id, build_id, path)
    SELECT fl.rowid, f.file_id, f.build_id, f.path
    FROM path_fts f JOIN files fl ON fl.id = f.file_id AND fl.build_id = f.build_id;
DROP TABLE path_fts;
ALTER TABLE path_fts_v3 RENAME TO path_fts;
"#,
    },
    Migration {
        version: 4,
        name: "manual_link_sections",
        sql: r#"
-- Knowledge linker (RUST-08): manual links may pin a specific chunk
-- (ordinal within the document). Optional parts use the -1 sentinel so the
-- natural key is enforced (SQLite treats NULLs in UNIQUE as distinct).
CREATE TABLE manual_links_v4 (
    id TEXT PRIMARY KEY,
    link_type TEXT NOT NULL,
    entity_qualified_name TEXT NOT NULL,
    document_rel_path TEXT NOT NULL,
    attachment_index INTEGER NOT NULL DEFAULT -1,
    chunk_ordinal INTEGER NOT NULL DEFAULT -1,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (link_type, entity_qualified_name, document_rel_path, attachment_index, chunk_ordinal)
);
INSERT INTO manual_links_v4 (id, link_type, entity_qualified_name, document_rel_path,
        attachment_index, chunk_ordinal, note, created_at)
    SELECT id, link_type, entity_qualified_name, document_rel_path,
        COALESCE(attachment_index, -1), -1, note, created_at
    FROM manual_links;
DROP TABLE manual_links;
ALTER TABLE manual_links_v4 RENAME TO manual_links;
CREATE INDEX idx_links_resolver ON cross_links(build_id, resolver);
"#,
    },
    Migration {
        version: 5,
        name: "embeddings",
        sql: r#"
-- Embeddings (RUST-09). Vectors are build-scoped like every other derived
-- row and stamped with the model fingerprint (model id, revision, asset
-- checksums, preprocessing version) so a model change is detected and
-- re-embedded explicitly. V1 vectors are never imported.
CREATE TABLE embeddings (
    build_id TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    dims INTEGER NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (build_id, subject_type, subject_id)
);
CREATE INDEX idx_embeddings_file ON embeddings(build_id, file_id);
-- Cross-build cache: exact text hash + model fingerprint + text version.
CREATE TABLE embedding_cache (
    text_hash TEXT NOT NULL,
    model_fingerprint TEXT NOT NULL,
    embedding_text_version TEXT NOT NULL,
    dims INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (text_hash, model_fingerprint, embedding_text_version)
);
"#,
    },
];
