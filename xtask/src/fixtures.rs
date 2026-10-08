//! `cargo xtask p0-corpus`: a deterministic multi-repository corpus for the
//! P0 scale, fairness and retrieval scenarios (ADR 0033).
//!
//! Every source `repo-NNN` holds C# services whose classes call a class of
//! the next repository (cross-repository symbols), Markdown design notes
//! in English and Greek that name those classes, and (with
//! `--with-documents`) copies of the binary document fixtures: PDFs, a
//! scanned PDF and EML messages with attachments. The same arguments always
//! produce byte-identical trees, so a benchmark run is reproducible from
//! `(sources, files, seed)`.

use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

pub struct CorpusArgs {
    pub sources: usize,
    pub files: usize,
    pub seed: u64,
    pub with_documents: bool,
    pub out: PathBuf,
}

impl CorpusArgs {
    pub fn parse(mut args: impl Iterator<Item = String>) -> Result<Self> {
        let mut a = CorpusArgs {
            sources: 150,
            files: 200_000,
            seed: 1,
            with_documents: false,
            out: PathBuf::new(),
        };
        while let Some(flag) = args.next() {
            let mut value = || args.next().with_context(|| format!("{flag} needs a value"));
            match flag.as_str() {
                "--sources" => a.sources = value()?.parse()?,
                "--files" => a.files = value()?.parse()?,
                "--seed" => a.seed = value()?.parse()?,
                "--with-documents" => a.with_documents = true,
                "--out" => a.out = PathBuf::from(value()?),
                other => bail!("unknown option {other}"),
            }
        }
        if a.out.as_os_str().is_empty() {
            bail!("usage: cargo xtask p0-corpus --out DIR [--sources N] [--files N] [--seed N] [--with-documents]");
        }
        if a.sources == 0 || a.files < a.sources {
            bail!("need at least one source and one file per source");
        }
        Ok(a)
    }
}

/// SplitMix64: tiny, deterministic and good enough for corpus variety.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn pick<'a>(&mut self, items: &[&'a str]) -> &'a str {
        items[(self.next() % items.len() as u64) as usize]
    }
}

const DOMAINS: &[&str] = &[
    "Billing",
    "Ledger",
    "Payment",
    "Invoice",
    "Customer",
    "Account",
    "Settlement",
    "Refund",
    "Audit",
    "Report",
    "Catalog",
    "Order",
    "Shipment",
    "Tax",
    "Pricing",
    "Inventory",
];
const VERBS: &[&str] = &[
    "Create",
    "Validate",
    "Settle",
    "Reconcile",
    "Load",
    "Publish",
    "Archive",
];
const GREEK: &[&str] = &[
    "Η υπηρεσία επεξεργάζεται τις πληρωμές πελατών.",
    "Ο λογαριασμός ενημερώνεται μετά τον συμψηφισμό.",
    "Τα τιμολόγια αρχειοθετούνται κάθε μήνα.",
    "Η αναφορά ελέγχου δημιουργείται αυτόματα.",
];
const ENGLISH: &[&str] = &[
    "The service validates every request before it is persisted.",
    "Settlement runs nightly and reconciles the ledger with the bank.",
    "Refunds are published to the audit stream within one minute.",
    "Pricing rules are loaded from the catalog at startup.",
];

/// The class every file of `repo` in slot `n` defines.
pub fn class_name(repo: usize, n: usize) -> String {
    format!(
        "{}Service{repo:03}x{n}",
        DOMAINS[(repo + n) % DOMAINS.len()]
    )
}

fn csharp(repo: usize, n: usize, sources: usize, rng: &mut Rng) -> String {
    let next_repo = (repo + 1) % sources;
    let class = class_name(repo, n);
    let callee = class_name(next_repo, n % 4);
    let verb = rng.pick(VERBS);
    let mut s = String::new();
    let _ = writeln!(s, "namespace Corp.Repo{repo:03}.Services");
    s.push_str("{\n");
    let _ = writeln!(
        s,
        "    /// <summary>{verb}s records for repository {repo:03}.</summary>"
    );
    let _ = writeln!(s, "    public class {class}");
    s.push_str("    {\n");
    let _ = writeln!(
        s,
        "        private readonly Corp.Repo{next_repo:03}.Services.{callee} _next;"
    );
    let _ = writeln!(s, "        public int {verb}(int amount)");
    s.push_str("        {\n");
    let _ = writeln!(
        s,
        "            var total = _next.{verb}(amount) + {};",
        rng.next() % 997
    );
    s.push_str("            return total;\n        }\n    }\n}\n");
    s
}

fn markdown(repo: usize, n: usize, rng: &mut Rng) -> String {
    let class = class_name(repo, n);
    let mut s = format!("# {class} design note\n\n");
    let _ = writeln!(
        s,
        "{} `{class}` is owned by repository {repo:03}.\n",
        rng.pick(ENGLISH)
    );
    let _ = writeln!(s, "## Λειτουργία\n\n{} Κλάση: {class}.\n", rng.pick(GREEK));
    let _ = writeln!(s, "## Operations\n\n{}\n", rng.pick(ENGLISH));
    s
}

const DOCUMENT_FIXTURES: &[&str] = &[
    "sample.pdf",
    "five_pages.pdf",
    "scanned.pdf",
    "email_with_attachments.eml",
    "sample.eml",
    "handbook.md",
];

/// Writes the corpus and returns the number of files written.
pub fn generate(repo_root: &Path, a: &CorpusArgs) -> Result<usize> {
    let per_source = a.files / a.sources;
    let mut written = 0;
    for repo in 0..a.sources {
        let mut rng = Rng(a.seed ^ (repo as u64).wrapping_mul(0x2545_F491_4F6C_DD1D));
        let root = a.out.join(format!("repo-{repo:03}"));
        fs::create_dir_all(root.join("src")).with_context(|| root.display().to_string())?;
        fs::create_dir_all(root.join("docs"))?;
        let mut docs_budget = if a.with_documents {
            DOCUMENT_FIXTURES.len()
        } else {
            0
        };
        for n in 0..per_source {
            // Four code files for every design note.
            if n % 5 == 4 {
                fs::write(
                    root.join(format!("docs/note-{n:06}.md")),
                    markdown(repo, n, &mut rng),
                )?;
            } else {
                let dir = root.join(format!("src/m{:03}", n / 500));
                fs::create_dir_all(&dir)?;
                fs::write(
                    dir.join(format!("{}.cs", class_name(repo, n))),
                    csharp(repo, n, a.sources, &mut rng),
                )?;
            }
            written += 1;
        }
        // Binary documents only in every tenth repository, so the mix
        // stays realistic (most repositories are code).
        if repo % 10 == 0 {
            while docs_budget > 0 {
                docs_budget -= 1;
                let name = DOCUMENT_FIXTURES[docs_budget];
                let from = repo_root.join("fixtures/documents").join(name);
                fs::copy(&from, root.join("docs").join(name))
                    .with_context(|| from.display().to_string())?;
                written += 1;
            }
        }
    }
    Ok(written)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree(dir: &Path) -> Vec<(String, Vec<u8>)> {
        let mut out = Vec::new();
        let mut stack = vec![dir.to_path_buf()];
        while let Some(d) = stack.pop() {
            for e in fs::read_dir(&d).unwrap() {
                let p = e.unwrap().path();
                if p.is_dir() {
                    stack.push(p);
                } else {
                    let rel = p.strip_prefix(dir).unwrap().to_string_lossy().into_owned();
                    out.push((rel, fs::read(&p).unwrap()));
                }
            }
        }
        out.sort();
        out
    }

    #[test]
    fn corpus_is_deterministic_and_cross_references_repositories() {
        let root = crate::audit::repo_root().unwrap();
        let gen = |dir: &Path| {
            generate(
                &root,
                &CorpusArgs {
                    sources: 3,
                    files: 30,
                    seed: 7,
                    with_documents: true,
                    out: dir.to_path_buf(),
                },
            )
            .unwrap()
        };
        let a = tempfile::tempdir().unwrap();
        let b = tempfile::tempdir().unwrap();
        let n = gen(a.path());
        assert_eq!(n, gen(b.path()));
        assert_eq!(tree(a.path()), tree(b.path()));
        assert_eq!(n, 30 + DOCUMENT_FIXTURES.len());
        let cs = fs::read_to_string(
            a.path()
                .join("repo-002/src/m000")
                .join(format!("{}.cs", class_name(2, 0))),
        )
        .unwrap();
        // repo 2 calls into repo 0 (wraps around).
        assert!(cs.contains("Corp.Repo000.Services"), "{cs}");
        let md = fs::read_to_string(a.path().join("repo-001/docs/note-000004.md")).unwrap();
        assert!(md.contains("Λειτουργία"));
    }
}
