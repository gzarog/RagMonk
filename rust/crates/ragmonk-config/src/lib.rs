//! RagMonk configuration (plan phase RUST-01), ported from
//! `ragmonk.core.config` with byte-level compatibility as the goal:
//!
//! * [`pyvalue`]: the Python object model a YAML document loads into;
//! * [`yaml_load`]: PyYAML `safe_load` semantics (YAML 1.1 implicit
//!   resolvers: `yes`/`on` booleans, `012` octal, `1:30` sexagesimal,
//!   timestamps, merge keys) on top of the `yaml-rust2` event parser;
//! * [`coerce`]: pydantic v2 lax-mode coercion and error messages;
//! * [`model`]: the typed schema, defaults and validators;
//! * [`yaml_emit`]: PyYAML `safe_dump(sort_keys=False)` output;
//! * [`loader`]: layered loading (defaults < user < project < env < CLI),
//!   `config get/set` semantics and `write_user_config`.
//!
//! Every behavior is replayed against fixtures generated from the Python
//! reference (`rust/compat/golden/core_config.json`).

pub mod coerce;
pub mod loader;
pub mod model;
pub mod pyvalue;
pub mod yaml_emit;
pub mod yaml_load;

pub use loader::{load_config, write_user_config, LoadOptions};
pub use model::RagMonkConfig;
