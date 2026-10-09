//! Tree-sitter code intelligence: language detection, the
//! query-driven extractor, reference resolution, framework heuristics,
//! the code processor for the indexer and graph traversal.

pub mod extract;
pub mod framework;
pub mod graph;
pub mod lang;
pub mod process;
pub mod resolve;

use std::sync::Arc;

use ragmonk_indexing::coordinator::{RawProcessor, Registry};

/// The registry with Rust-native code extraction only (documents are
/// recorded raw).
pub fn registry() -> Registry {
    registry_with_relationships(true)
}

/// [`registry`], optionally without relationship extraction and
/// cross-file resolution (`indexing.relationships_enabled: false`).
pub fn registry_with_relationships(relationships: bool) -> Registry {
    let mut r = Registry::raw();
    r.code = Arc::new(process::CodeProcessor { relationships });
    r.document = Arc::new(RawProcessor);
    r.versions.parser_version = process::CODE_DERIVATION_VERSION.into();
    if relationships {
        r.finalizers.push(Arc::new(process::CrossFileResolver));
    } else {
        // A different output shape, so a distinct derivation identity.
        r.versions.parser_version.push_str("+no-relationships");
    }
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disabling_relationships_drops_resolver_and_changes_identity() {
        let on = registry_with_relationships(true);
        let off = registry_with_relationships(false);
        assert_eq!(on.finalizers.len(), 1);
        assert!(off.finalizers.is_empty());
        assert_ne!(on.versions.parser_version, off.versions.parser_version);
    }
}
