//! RagMonk indexing core.
//!
//! * [`classify`], [`ignore`], [`scan`], [`fingerprint`]: file discovery,
//!   ported from `ragmonk.sources.{detector,ignore,scanner,fingerprint}`.
//! * [`diff`]: metadata-first change classification with conditional
//!   hashing, rename detection and incomplete/offline-scan safety
//!   (with the coordinator's reconciliation).
//! * [`retry`]: the exponential backoff policy.
//! * [`lock`]: the bounded cross-process run lock with owner metadata
//!   without `unsafe`.
//! * [`coordinator`]: per-source builds with bounded parallel prepare,
//!   a single writer, per-source failure isolation and atomic publication.

pub mod classify;
pub mod coordinator;
pub mod daemon;
pub mod diff;
pub mod fingerprint;
pub mod ignore;
pub mod lock;
pub mod progress;
pub mod retry;
pub mod scan;
pub mod status;
pub mod watcher;
