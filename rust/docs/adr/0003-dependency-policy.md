# ADR 0003 — Dependency policy

Status: accepted (RUST-00)

* Shared versions live in `[workspace.dependencies]`; crates use `.workspace = true`.
* Preferred stack is the plan's `recommended_stack` (tokio, clap, serde,
  thiserror/anyhow, tracing, reqwest+rustls, rusqlite, notify, tree-sitter,
  tokenizers, ort, axum, docling.rs behind RagMonk's own trait).
* A new dependency needs: maintained upstream, permissive license compatible
  with MIT, builds on Windows/Linux/macOS without system packages where
  possible (e.g. `rusqlite` `bundled`), and a reason it beats a few lines of
  local code.
* `anyhow` only at process/application boundaries; libraries use `thiserror`.
* TLS uses rustls; no OpenSSL requirement.
* `Cargo.lock` is committed (the workspace ships binaries).
* Toolchain is pinned by `rust/rust-toolchain.toml`; MSRV = that version.
* Python packages may be used only by migration/test tooling (the compat
  harness drives the Python reference as a subprocess) and never by the
  production runtime.
