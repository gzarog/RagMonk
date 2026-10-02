"""Generate RUST-04 scan/diff golden fixtures from the Python reference.

    python rust/compat/tools/gen_indexing_golden.py

Builds the tree described by TREE, records the reference scanner's output
(relative path + kind) and then runs `ragmonk index` through STEPS,
recording the per-run counts. Writes rust/compat/golden/indexing.json; the
Rust tests rebuild the same tree and replay the same steps.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ragmonk.security.path_guard import PathGuard
from ragmonk.sources.detector import classify
from ragmonk.sources.ignore import IgnoreMatcher
from ragmonk.sources.scanner import scan

OUT = Path(__file__).resolve().parents[1] / "golden" / "indexing.json"

TREE = {
    ".gitignore": "# comment\n*.log\nbuild-out/\n/docs/private/*\n",
    ".ragmonkignore": "*.tmp\n",
    "src/app.py": "def main():\n    return 1\n",
    "src/util/helpers.ts": "export const x = 1;\n",
    "src/util/README.md": "# Helpers\n",
    "src/util/data.CSV": "a,b\n1,2\n",
    "src/node_modules/pkg/index.js": "module.exports = 1;\n",
    "src/.env": "SECRET=1\n",
    "src/server.key": "k\n",
    "src/notes.log": "log\n",
    "src/keep.log": "kept by include\n",
    "src/scratch.tmp": "tmp\n",
    "build-out/gen.py": "x = 1\n",
    "docs/guide.md": "# Guide\n",
    "docs/private/secret.md": "# private\n",
    "docs/private.md": "# not private\n",
    "docs/archive.tar.gz": "bin\n",
    "Makefile": "all:\n",
    "lib/Code.CS": "class A {}\n",
    "lib/script.SH": "echo hi\n",
    "target/debug/out.rs": "fn main() {}\n",
    "credentials.json": "{}\n",
}
INCLUDE = ["keep.log"]
EXCLUDE = ["Makefile"]

STEPS = [
    {"name": "cold"},
    {"name": "warm"},
    {"name": "edit", "write": {"src/app.py": "def main():\n    return 2\n"}},
    {"name": "touch", "touch": ["docs/guide.md"]},
    {"name": "rename", "rename": {"src/util/helpers.ts": "src/util/helpers2.ts"}},
    {"name": "delete", "delete": ["lib/script.SH"]},
    {"name": "add", "write": {"src/new_mod.py": "y = 2\n"}},
    {"name": "warm_again"},
]

COUNT_RE = re.compile(
    r"scanned=(\d+) new=(\d+) changed=(\d+) unchanged=(\d+)\s+moved=(\d+) deleted=(\d+)"
)


def build_tree(root: Path) -> None:
    for rel, content in TREE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def apply_step(root: Path, step: dict) -> None:
    for rel, content in step.get("write", {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for rel in step.get("touch", []):
        p = root / rel
        st = p.stat()
        os.utime(p, (st.st_atime, st.st_mtime + 10))
    for old, new in step.get("rename", {}).items():
        (root / old).rename(root / new)
    for rel in step.get("delete", []):
        (root / rel).unlink()


def run_cli(home: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAGMONK_")}
    env.update(
        {
            "RAGMONK_HOME": str(home),
            "RAGMONK_UPDATES__ENABLED": "false",
            "COLUMNS": "200",
            "NO_COLOR": "1",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "ragmonk.cli.main", *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ragmonk_idx_golden_") as tmp:
        root = Path(tmp) / "src_root"
        home = Path(tmp) / "home"
        root.mkdir()
        build_tree(root)
        resolved = root.resolve()
        matcher = IgnoreMatcher(root=resolved, extra_patterns=EXCLUDE, include_patterns=INCLUDE)
        scanned = sorted(
            (
                {
                    "rel_path": Path(sf.path).relative_to(resolved).as_posix(),
                    "kind": str(classify(Path(sf.path))),
                }
                for sf in scan(resolved, guard=PathGuard([resolved]), ignore_matcher=matcher)
            ),
            key=lambda d: d["rel_path"],
        )
        run_cli(home, "init")
        add_args = ["source", "add", str(root)]
        for pat in INCLUDE:
            add_args += ["--include", pat]
        for pat in EXCLUDE:
            add_args += ["--exclude", pat]
        run_cli(home, *add_args)
        steps = []
        for step in STEPS:
            apply_step(root, step)
            time.sleep(0.01)
            out = run_cli(home, "index")
            flat = " ".join(out.split())
            m = COUNT_RE.search(flat)
            assert m, out
            keys = ["scanned", "new", "changed", "unchanged", "moved", "deleted"]
            steps.append({**step, "counts": dict(zip(keys, map(int, m.groups()), strict=True))})
    payload = {
        "tree": TREE,
        "include": INCLUDE,
        "exclude": EXCLUDE,
        "scan": scanned,
        "steps": steps,
    }
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(payload["scan"], indent=1))
    for s in steps:
        print(s["name"], s["counts"])


if __name__ == "__main__":
    main()
