//! Retrieval for RagMonk V2 (RUST-10).
//!
//! * [`lexical`]: exact / qualified / alias symbol lookups, document title
//!   and FTS hits, and path hits, tiered and merged exactly like the
//!   reference's `retrieval/lexical.py`.
//! * [`hybrid`]: the reference's `merger.py` + `reranker.py`: lexical and
//!   semantic candidates deduplicated per `(kind, id)`. Exact matches are
//!   pinned; everything else is ordered by Reciprocal Rank Fusion. An
//!   optional cross-encoder pass reorders the top of the list.
//!
//! Search runs over one or more [`Corpus`] values (a project store plus the
//! build to read), so multi-source search is the caller's list.

pub mod hybrid;
pub mod lexical;

use ragmonk_storage::knowledge::ProjectStore;

/// One searchable source: its store and the build to read (normally the
/// active build).
#[derive(Clone, Copy)]
pub struct Corpus<'a> {
    pub store: &'a ProjectStore,
    pub build_id: &'a str,
}

impl std::fmt::Debug for Corpus<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Corpus")
            .field("source_id", &self.store.source_id())
            .field("build_id", &self.build_id)
            .finish()
    }
}

/// A search failure (storage error); "nothing found" is an empty result.
#[derive(Debug, thiserror::Error)]
#[error("search failed: {0}")]
pub struct SearchError(pub String);

impl From<ragmonk_storage::error::StorageError> for SearchError {
    fn from(e: ragmonk_storage::error::StorageError) -> Self {
        Self(e.to_string())
    }
}
