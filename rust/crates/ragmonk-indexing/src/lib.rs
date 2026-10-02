//! RagMonk indexing core (plan phase RUST-04).
//!
//! * [`classify`], [`ignore`], [`scan`], [`fingerprint`]: file discovery,
//!   ported from `ragmonk.sources.{detector,ignore,scanner,fingerprint}`.
//! * [`diff`]: metadata-first change classification with conditional
//!   hashing, rename detection and incomplete/offline-scan safety
//!   (`ragmonk.indexing.incremental` + the coordinator's reconciliation).
//! * [`retry`]: the reference backoff policy.
//! * [`lock`]: the bounded cross-process run lock with owner metadata
//!   (`ragmonk.core.lifecycle.RunLock`), without `unsafe`.
//! * [`coordinator`]: per-source V2 builds with bounded parallel prepare,
//!   a single writer, per-source failure isolation and atomic publication.

pub mod classify;
pub mod coordinator;
pub mod diff;
pub mod fingerprint;
pub mod ignore;
pub mod lock;
pub mod retry;
pub mod scan;
