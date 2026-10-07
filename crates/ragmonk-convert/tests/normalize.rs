//! The Rust normalizer reproduces the Python normalizer exactly on the
//! reference's own Docling documents (golden/docling.json).

use std::path::Path;

use ragmonk_convert::normalize::normalize;
use serde_json::Value;

#[test]
fn normalizer_matches_reference_on_reference_docling_json() {
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/expected/docling.json");
    let g: Value = serde_json::from_str(&std::fs::read_to_string(p).unwrap()).unwrap();
    let mut failures = Vec::new();
    for (name, e) in g.as_object().unwrap() {
        let got = normalize(&e["docling"], e["format"] == "pdf");
        let got = serde_json::to_value(&got).unwrap();
        if got != e["normalized"] {
            failures.push(format!(
                "{name}:\n rust   {got}\n python {}",
                e["normalized"]
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}
