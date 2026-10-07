//! Structured logging and redaction (plan phase RUST-01).
//!
//! * [`redact`] ports `ragmonk.backends.factory.redact_urls_in_text` /
//!   `redact_url` and `ragmonk.core.config.url_has_userinfo`, including
//!   Python `urllib.parse.urlsplit` semantics.
//! * [`logging`] writes the same JSON-lines records as
//!   `ragmonk.telemetry.logging.JsonFormatter` to `<home>/logs/ragmonk.log`.

pub mod logging;
pub mod redact;
pub mod urlsplit;
