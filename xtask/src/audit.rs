//! `cargo xtask clean-slate-audit`: keeps migration, legacy-format,
//! V1/V2 product naming and Python tooling out of the repository.
//!
//! RagMonk 1.0 is fresh-install / fresh-index only (see the clean-slate
//! ADR). Every tracked text file is scanned line by line against [`RULES`].
//! A finding is accepted only when it is
//!
//! * covered by [`ALLOWED_PATHS`] (Python as an *indexed source language*:
//!   the parser wiring and `.py` input fixtures, nothing else),
//! * reduced to nothing by [`NEUTRAL_TOKENS`] (model ids such as
//!   `all-MiniLM-L6-v2`, semver tags, the Python tree-sitter grammar), or
//! * inside a `clean-slate-audit: allow-start` / `allow-end` block (the
//!   policy text that must name what it forbids), or
//! * counted in the transition baseline `audit/clean-slate-baseline.tsv`.
//!
//! The baseline is shrink-only: a count above it fails, and a count below
//! it also fails until the baseline is regenerated with
//! `--update-baseline`, so every cleanup phase locks in its progress. The
//! release gate runs with `--require-empty-baseline`.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};
use regex::Regex;

pub const BASELINE: &str = "audit/clean-slate-baseline.tsv";

#[derive(Default)]
pub struct Options {
    pub update_baseline: bool,
    pub require_empty_baseline: bool,
}

/// `(id, description, pattern)`; patterns run on a line after the
/// neutral tokens are blanked out.
const RULES: &[(&str, &str, &str)] = &[
    (
        "migration",
        "schema/data migration or import of a predecessor installation",
        r"(?i)migrat|schema_migrations|migration_log|v1_import|\bimport_v1",
    ),
    (
        "version-naming",
        "V1/V2 product, schema, path or index naming",
        r"(?i)(?:^|[^a-z0-9])v[12](?:[^a-z0-9]|$)|v[12]_|_v[12]\b|rust-v\b|rust-v[0-9*]|(?-i:V[12][A-Z][a-z]|[a-z]V[12]\b)",
    ),
    (
        "legacy",
        "legacy format/index/home handling",
        r"(?i)legacy|preflight",
    ),
    (
        "rewrite",
        "Python-to-Rust rewrite, cutover or differential-compatibility harness",
        r"(?i)rust[ _-]rewrite|cutover|differential|compat harness|rust/compat|python reference|python baseline",
    ),
    (
        "python",
        "Python runtime/tooling/semantics outside source-language support",
        r"(?i)python|setup-python|\bvenv\b|\.venv|pydantic|pyyaml|\bpip3?\b",
    ),
    (
        "python-file",
        "Python file outside source-language fixtures",
        r"\.pyi?$",
    ),
];

/// Substrings that look like a forbidden term but name something current.
const NEUTRAL_TOKENS: &[&str] = &[
    // Pinned model identifiers.
    r"(?i)all-MiniLM-L6-v2",
    r"(?i)ms-marco-MiniLM-L-?6-v2",
    // Semantic-version tags (`v1.0.0`).
    r"\bv[0-9]+\.[0-9]+",
    // The Python grammar RagMonk uses to index Python source.
    r"tree[-_]sitter[-_]python",
];

/// `(rule, path prefix or exact path)`: Python as an indexed language.
const ALLOWED_PATHS: &[(&str, &str)] = &[
    ("python", "crates/ragmonk-code/src/lang.rs"),
    ("python", "crates/ragmonk-code/src/framework.rs"),
    ("python", "crates/ragmonk-code/queries/python.scm"),
    ("python", "fixtures/code/python/"),
    ("python", "fixtures/corpus/languages/python/"),
    ("python-file", "fixtures/"),
];

/// Files exempt as a whole: this audit's own pattern definitions.
fn self_exempt(path: &str) -> bool {
    path.ends_with("xtask/src/audit.rs") || path == BASELINE
}

const ALLOW_START: &str = "clean-slate-audit: allow-start";
const ALLOW_END: &str = "clean-slate-audit: allow-end";

type Counts = BTreeMap<(String, String), usize>;

pub fn run(opts: &Options) -> Result<()> {
    let root = repo_root()?;
    let rules: Vec<(&str, &str, Regex)> = RULES
        .iter()
        .map(|(id, desc, pat)| Ok((*id, *desc, Regex::new(pat)?)))
        .collect::<Result<_>>()?;
    let neutral: Vec<Regex> = NEUTRAL_TOKENS
        .iter()
        .map(|p| Regex::new(p))
        .collect::<Result<_, _>>()?;

    let mut found = Counts::new();
    let mut examples: BTreeMap<(String, String), Vec<String>> = BTreeMap::new();
    for path in tracked_files(&root)? {
        if self_exempt(&path) {
            continue;
        }
        let mut hit = |rule: &str, line: String| {
            if allowed(rule, &path) {
                return;
            }
            let key = (rule.to_owned(), path.clone());
            *found.entry(key.clone()).or_default() += 1;
            let ex = examples.entry(key).or_default();
            if ex.len() < 3 {
                ex.push(line);
            }
        };
        // The path itself (file names such as `v1.rs`, `migrate_cmd.rs`).
        for (id, _, re) in &rules {
            if re.is_match(&blank(&path, &neutral)) {
                hit(id, format!("path: {path}"));
            }
        }
        let Some(text) = read_text(&root.join(&path)) else {
            continue;
        };
        let mut in_allow = false;
        for (n, line) in text.lines().enumerate() {
            if line.contains(ALLOW_START) {
                in_allow = true;
                continue;
            }
            if line.contains(ALLOW_END) {
                in_allow = false;
                continue;
            }
            if in_allow {
                continue;
            }
            let cleaned = blank(line, &neutral);
            for (id, _, re) in &rules {
                if *id != "python-file" && re.is_match(&cleaned) {
                    hit(id, format!("{}: {}", n + 1, line.trim()));
                }
            }
        }
    }

    let baseline_path = root.join(BASELINE);
    if opts.update_baseline {
        std::fs::write(&baseline_path, render_baseline(&found))
            .with_context(|| format!("writing {}", baseline_path.display()))?;
        println!(
            "wrote {BASELINE}: {} entries, {} findings",
            found.len(),
            found.values().sum::<usize>()
        );
        return Ok(());
    }

    let baseline = read_baseline(&baseline_path)?;
    let mut errors = String::new();
    for (key, &count) in &found {
        let allowed = baseline.get(key).copied().unwrap_or(0);
        if count > allowed {
            let desc = rules.iter().find(|r| r.0 == key.0).map_or("", |r| r.1);
            let _ = writeln!(
                errors,
                "{} [{}] {count} finding(s), baseline {allowed}: {desc}",
                key.1, key.0
            );
            for ex in &examples[key] {
                let _ = writeln!(errors, "    {ex}");
            }
        }
    }
    for (key, &allowed) in &baseline {
        let count = found.get(key).copied().unwrap_or(0);
        if count < allowed {
            let _ = writeln!(
                errors,
                "{} [{}] baseline {allowed} is stale ({count} now): the baseline only \
                 shrinks; run `cargo xtask clean-slate-audit --update-baseline`",
                key.1, key.0
            );
        }
    }
    if opts.require_empty_baseline && !baseline.is_empty() {
        let _ = writeln!(
            errors,
            "{BASELINE} still has {} entries; the release requires it to be empty",
            baseline.len()
        );
    }
    if !errors.is_empty() {
        bail!("clean-slate audit failed:\n{errors}");
    }
    println!(
        "clean-slate audit passed ({} baseline entries remain)",
        baseline.len()
    );
    Ok(())
}

fn allowed(rule: &str, path: &str) -> bool {
    ALLOWED_PATHS
        .iter()
        .any(|(r, p)| *r == rule && (path == *p || (p.ends_with('/') && path.starts_with(p))))
}

fn blank(line: &str, neutral: &[Regex]) -> String {
    neutral.iter().fold(line.to_owned(), |acc, re| {
        re.replace_all(&acc, " ").into_owned()
    })
}

fn repo_root() -> Result<PathBuf> {
    let out = Command::new("git")
        .args(["rev-parse", "--show-toplevel"])
        .output()
        .context("running git rev-parse")?;
    if !out.status.success() {
        bail!("not inside a git checkout");
    }
    Ok(PathBuf::from(String::from_utf8(out.stdout)?.trim()))
}

fn tracked_files(root: &Path) -> Result<Vec<String>> {
    let out = Command::new("git")
        .args(["ls-files", "-z"])
        .current_dir(root)
        .output()
        .context("running git ls-files")?;
    if !out.status.success() {
        bail!("git ls-files failed");
    }
    Ok(out
        .stdout
        .split(|b| *b == 0)
        .filter(|p| !p.is_empty())
        .map(|p| String::from_utf8_lossy(p).into_owned())
        .collect())
}

fn read_text(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    if bytes.contains(&0) {
        return None;
    }
    String::from_utf8(bytes).ok()
}

fn render_baseline(found: &Counts) -> String {
    let mut out = String::from(
        "# Clean-slate transition baseline (shrink-only; must be empty at release).\n\
         # Regenerate with `cargo xtask clean-slate-audit --update-baseline`.\n\
         # rule\tcount\tpath\n",
    );
    for ((rule, path), count) in found {
        let _ = writeln!(out, "{rule}\t{count}\t{path}");
    }
    out
}

fn read_baseline(path: &Path) -> Result<Counts> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Counts::new()),
        Err(e) => return Err(e).with_context(|| format!("reading {}", path.display())),
    };
    let mut counts = Counts::new();
    for (n, line) in text.lines().enumerate() {
        if line.starts_with('#') || line.trim().is_empty() {
            continue;
        }
        let mut cols = line.splitn(3, '\t');
        let (Some(rule), Some(count), Some(file)) = (cols.next(), cols.next(), cols.next()) else {
            bail!("{BASELINE}:{}: expected `rule<TAB>count<TAB>path`", n + 1);
        };
        let count = count
            .parse()
            .with_context(|| format!("{BASELINE}:{}: bad count", n + 1))?;
        counts.insert((rule.to_owned(), file.to_owned()), count);
    }
    Ok(counts)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matches(rule: &str, line: &str) -> bool {
        let neutral: Vec<Regex> = NEUTRAL_TOKENS
            .iter()
            .map(|p| Regex::new(p).unwrap())
            .collect();
        let pat = RULES.iter().find(|r| r.0 == rule).unwrap().2;
        Regex::new(pat).unwrap().is_match(&blank(line, &neutral))
    }

    #[test]
    fn version_naming_ignores_models_and_semver() {
        assert!(matches("version-naming", "pub struct V2Layout;"));
        assert!(matches("version-naming", "let prefix = \"ragmonk-v2\";"));
        assert!(matches("version-naming", "mod v1;"));
        assert!(matches("version-naming", "tags: rust-v*"));
        assert!(!matches(
            "version-naming",
            "models/all-MiniLM-L6-v2/model.safetensors"
        ));
        assert!(!matches(
            "version-naming",
            "cross-encoder/ms-marco-MiniLM-L-6-v2"
        ));
        assert!(!matches("version-naming", "release v1.0.0"));
        assert!(!matches("version-naming", "uses: actions/checkout@v4"));
    }

    #[test]
    fn python_grammar_is_neutral() {
        assert!(!matches("python", "tree-sitter-python = \"0.23\""));
        assert!(matches("python", "- uses: actions/setup-python@v5"));
        assert!(matches("python", "python3 scripts/check_branding.py"));
    }

    #[test]
    fn python_fixtures_are_allowed_only_under_fixtures() {
        assert!(allowed("python-file", "fixtures/code/python/animals.py"));
        assert!(!allowed("python-file", "scripts/check_branding.py"));
        assert!(!allowed(
            "python",
            "crates/ragmonk-config/src/value.rs"
        ));
    }
}
