#![allow(dead_code)]

use std::path::Path;

use ragmonk_core::models::SourceType;
use ragmonk_core::paths::Home;
use ragmonk_storage::control::{ControlPlane, NewSource, SourceOrigin, SourceRecord};
use ragmonk_storage::V2Layout;

pub fn home(dir: &Path) -> Home {
    Home::new(dir.join("home"))
}

pub fn control(home: &Home) -> ControlPlane {
    ControlPlane::open(&V2Layout::new(home), 8).unwrap().0
}

pub fn add_source(
    cp: &mut ControlPlane,
    root: &Path,
    include: &[&str],
    exclude: &[&str],
) -> SourceRecord {
    let canonical = ragmonk_core::paths::resolve(root)
        .unwrap()
        .to_string_lossy()
        .into_owned();
    cp.add_source(&NewSource {
        canonical_path: canonical,
        source_type: SourceType::Local,
        enabled: true,
        include_patterns: include.iter().map(|s| s.to_string()).collect(),
        exclude_patterns: exclude.iter().map(|s| s.to_string()).collect(),
        origin: SourceOrigin::V2,
        created_at: None,
    })
    .unwrap()
    .0
}

pub fn write(root: &Path, rel: &str, content: &str) {
    let p = root.join(rel);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::write(p, content).unwrap();
}

pub fn bump_mtime(path: &Path, secs: u64) {
    let meta = std::fs::metadata(path).unwrap();
    let t = meta.modified().unwrap() + std::time::Duration::from_secs(secs);
    std::fs::File::options()
        .write(true)
        .open(path)
        .unwrap()
        .set_modified(t)
        .unwrap();
}
