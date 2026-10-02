//! Python-vs-Rust differential harness for the RagMonk rewrite.
//!
//! A [`manifest::Manifest`] lists scenarios: fixture files copied into an
//! isolated work directory, then a sequence of CLI steps run with
//! `RAGMONK_HOME` pointed inside that directory. [`runner`] executes a
//! manifest against one implementation (the Python reference or the Rust
//! candidate), [`canon`] turns raw output into a canonical form, and
//! [`compare`] diffs two canonical captures with float tolerance.

pub mod canon;
pub mod compare;
pub mod manifest;
pub mod runner;
pub mod sqlite_inventory;
