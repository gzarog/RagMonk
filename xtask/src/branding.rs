//! `cargo xtask branding-audit`: the product's former name may not
//! appear anywhere in tracked files, except inside a
//! `branding-audit-allow-start` / `branding-audit-allow-end` block (the
//! single note that explains old installations are unsupported).

use anyhow::{bail, Result};
use regex::Regex;

use crate::audit::{read_text, repo_root, tracked_files};

/// Built from parts so this file does not contain the name it forbids.
fn forbidden() -> Regex {
    Regex::new(&format!("(?i){}{}", "rag", "pilot")).expect("static regex")
}

const ALLOW_START: &str = "branding-audit-allow-start";
const ALLOW_END: &str = "branding-audit-allow-end";

/// `(path, line number, line)` of every finding in `text`.
fn scan(path: &str, text: &str, re: &Regex) -> Vec<(String, usize, String)> {
    let mut findings = Vec::new();
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
        if !in_allow && re.is_match(line) {
            findings.push((path.to_owned(), n + 1, line.trim().to_owned()));
        }
    }
    findings
}

pub fn run() -> Result<()> {
    let root = repo_root()?;
    let re = forbidden();
    let mut findings = Vec::new();
    for path in tracked_files(&root)? {
        if re.is_match(&path) {
            findings.push((path.clone(), 0, "(file name)".to_owned()));
        }
        if let Some(text) = read_text(&root.join(&path)) {
            findings.extend(scan(&path, &text, &re));
        }
    }
    if findings.is_empty() {
        println!("branding audit passed");
        return Ok(());
    }
    for (path, line, text) in &findings {
        println!("{path}:{line}: {text}");
    }
    bail!(
        "branding audit failed: {} occurrence(s) of the former product name",
        findings.len()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allow_blocks_are_exempt() {
        let re = forbidden();
        let old = format!("{}{}", "RAG", "pilot");
        let text =
            format!("fine\n{old} here\n<!-- {ALLOW_START} -->\n{old} note\n<!-- {ALLOW_END} -->\n");
        let f = scan("x.md", &text, &re);
        assert_eq!(f.len(), 1);
        assert_eq!(f[0].1, 2);
    }
}
