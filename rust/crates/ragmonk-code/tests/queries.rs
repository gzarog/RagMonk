//! The Rust crate ships copies of the reference's `.scm` queries. While the
//! Python reference is in the tree (until RUST-16), they must stay
//! identical except for the one documented Python grammar adaptation.

use std::path::Path;

#[test]
fn queries_are_the_reference_queries() {
    let ours = Path::new(env!("CARGO_MANIFEST_DIR")).join("queries");
    let reference = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../src/ragmonk/code/queries");
    if !reference.exists() {
        eprintln!("skipping: Python reference removed");
        return;
    }
    for lang in [
        "csharp",
        "go",
        "java",
        "javascript",
        "python",
        "rust",
        "typescript",
    ] {
        let read = |dir: &Path| {
            std::fs::read_to_string(dir.join(format!("{lang}.scm")))
                .unwrap()
                .replace("\r\n", "\n")
        };
        let (mine, theirs) = (read(&ours), read(&reference));
        if lang == "python" {
            let adapted = theirs.replace(
                "(class_definition\n  body: (block\n    (assignment left: (identifier) @field.name) @field.definition))",
                "",
            );
            assert_ne!(
                adapted, theirs,
                "reference field pattern changed; re-port it"
            );
            let start = mine.find(";\n; RagMonk Rust:").expect("adaptation note");
            let end = mine[start..].find("@field.definition)))").unwrap()
                + start
                + "@field.definition)))".len();
            let stripped = format!("{}{}", &mine[..start], &mine[end..]);
            assert_eq!(stripped, adapted, "{lang}");
        } else {
            assert_eq!(mine, theirs, "{lang}");
        }
    }
}
