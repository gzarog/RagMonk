//! RagMonk V2 server backends (V3 clean-slate plan, phase RUST-03).
//!
//! * [`schema`]: the V2 index set `{prefix}-v2-{source-state,files,code,
//!   documents,chunks,relationships}` with strict mappings, vector/HNSW
//!   settings fixed at creation and a `_meta.schema_version`.
//! * [`backend::ServerBackend`]: idempotent writes with deterministic V2
//!   IDs, bounded adaptive bulk batching, and atomic per-source build
//!   publication (an unpublished build is never returned by any read).
//! * [`legacy`]: discovery and explicitly confirmed deletion of Python-era
//!   RagMonk indexes. Normal V2 operation never reads them.
//!
//! OpenSearch and Elasticsearch share one adapter; [`engine::Engine`]
//! isolates the few mapping/query differences (vector field types).

pub mod backend;
pub mod bulk;
pub mod engine;
pub mod error;
pub mod legacy;
pub mod schema;
pub mod transport;

pub use backend::ServerBackend;
pub use error::BackendError;
