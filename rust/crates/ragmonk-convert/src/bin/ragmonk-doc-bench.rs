//! Document indexing benchmark.
//!
//! ```text
//! ragmonk-doc-bench FIXTURE_DIR [--copies N] [--out report.json] [--emit-only DIR]
//! ```
//!
//! Builds a corpus of N copies of each Markdown/HTML/TXT/DOCX/PPTX/XLSX
//! fixture, then times cold, warm and single-edit passes through the V2
//! coordinator with Rust-native conversion and chunking.

use std::path::{Path, PathBuf};
use std::time::Instant;

use ragmonk_core::models::SourceType;
use ragmonk_core::paths::Home;
use ragmonk_indexing::coordinator::{run_source, NoProgress, Options};
use ragmonk_storage::control::{ControlPlane, NewSource, SourceOrigin};
use ragmonk_storage::V2Layout;
use serde_json::json;

const KINDS: &[&str] = &[
    "handbook.md",
    "status.html",
    "long.txt",
    "document.docx",
    "presentation.pptx",
    "spreadsheet.xlsx",
];

fn emit(fixtures: &Path, root: &Path, copies: usize) {
    for i in 0..copies {
        let dir = root.join(format!("d{:02}", i % 20));
        std::fs::create_dir_all(&dir).expect("mkdir");
        for k in KINDS {
            let (stem, ext) = k.rsplit_once('.').expect("ext");
            std::fs::copy(fixtures.join(k), dir.join(format!("{stem}_{i}.{ext}"))).expect("copy");
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let arg = |n: &str| {
        args.iter()
            .position(|a| a == n)
            .and_then(|i| args.get(i + 1))
            .cloned()
    };
    let fixtures = PathBuf::from(args.get(1).expect("FIXTURE_DIR"));
    let copies: usize = arg("--copies").and_then(|v| v.parse().ok()).unwrap_or(100);
    if let Some(dir) = arg("--emit-only") {
        emit(&fixtures, Path::new(&dir), copies);
        return;
    }
    let tmp = std::env::temp_dir().join(format!("ragmonk-doc-bench-{}", std::process::id()));
    let root = tmp.join("source");
    emit(&fixtures, &root, copies);
    let home = Home::new(tmp.join("home"));
    let layout = V2Layout::new(&home);
    let (mut cp, _) = ControlPlane::open(&layout, 64).expect("control plane");
    let canonical = ragmonk_core::paths::resolve(&root)
        .expect("resolve")
        .to_string_lossy()
        .into_owned();
    let (src, _) = cp
        .add_source(&NewSource {
            canonical_path: canonical,
            source_type: SourceType::Local,
            enabled: true,
            include_patterns: vec![],
            exclude_patterns: vec![],
            origin: SourceOrigin::V2,
            created_at: None,
        })
        .expect("source");
    let cfg = ragmonk_config::RagMonkConfig::default();
    let reg = ragmonk_convert::registry(&cfg);
    let opts = Options::from_config(&cfg);
    let mut results = Vec::new();
    let mut run = |name: &str, cp: &mut ControlPlane| {
        let t = Instant::now();
        let r = run_source(&layout, cp, &src, &reg, &opts, &mut NoProgress).expect("run");
        let wall = t.elapsed().as_secs_f64();
        eprintln!(
            "{name:<16} wall={wall:.4}s indexed={} failed={}",
            r.indexed, r.failed
        );
        results.push(json!({"scenario": name, "wall_time_s": wall, "indexed": r.indexed, "failed": r.failed, "timings": r.timings}));
    };
    run("cold_index", &mut cp);
    run("warm_unchanged", &mut cp);
    std::fs::write(
        root.join("d00/handbook_0.md"),
        "# Edited\n\nChanged body text.\n",
    )
    .expect("edit");
    run("single_edit", &mut cp);
    let report = json!({
        "phase": "RUST-07", "implementation": "rust", "files": copies * KINDS.len(),
        "workers": opts.workers, "scenarios": results,
    });
    let text = serde_json::to_string_pretty(&report).expect("json");
    match arg("--out") {
        Some(p) => std::fs::write(p, text + "\n").expect("write"),
        None => println!("{text}"),
    }
    let _ = std::fs::remove_dir_all(&tmp);
}
