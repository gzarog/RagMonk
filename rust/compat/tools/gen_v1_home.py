"""Build a Python-era (V1) RagMonk home fixture with the reference CLI.

Run from the repository root with the Python reference installed:

    python rust/compat/tools/gen_v1_home.py

Writes rust/compat/fixtures/v1_home/: config.yaml, sources.db and each
source's projects/<id>/knowledge.db, after a full V1 index. Databases are
checkpointed and switched to rollback-journal mode so the fixture is a
self-contained set of files. Source paths inside are absolute paths of a
temporary directory that no longer exists afterwards (the "source path
missing" case is part of what the Rust import must handle).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "rust" / "compat" / "fixtures" / "v1_home"


def run(home: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAGMONK_")}
    env.update({"RAGMONK_HOME": str(home), "RAGMONK_UPDATES__ENABLED": "false"})
    subprocess.run([sys.executable, "-m", "ragmonk.cli.main", *args], check=True, env=env)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ragmonk_v1_fixture_") as tmp:
        root = Path(tmp)
        home = root / "home"
        code = root / "code_repo"
        docs = root / "docs_repo"
        shutil.copytree(REPO / "tests" / "fixtures" / "languages", code)
        docs.mkdir()
        for name in ("simple.md", "simple.txt", "email_with_attachments.eml"):
            shutil.copy(REPO / "tests" / "fixtures" / "documents" / name, docs / name)
        run(home, "init")
        run(home, "config", "set", "runtime.max_workers", "3")
        run(home, "source", "add", str(code), "--exclude", "**/rust/**")
        run(home, "source", "add", str(docs), "--include", "*.md", "--include", "*.eml")
        run(home, "index")
        # Disable the docs source after indexing: its V1 state stays populated.
        conn = sqlite3.connect(home / "sources.db")
        source_id = conn.execute("SELECT id FROM sources WHERE path = ?", (str(docs),)).fetchone()[
            0
        ]
        conn.close()
        run(home, "source", "disable", source_id)

        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        shutil.copy(home / "config.yaml", OUT / "config.yaml")
        for db in [home / "sources.db", *sorted((home / "projects").glob("*/knowledge.db"))]:
            rel = db.relative_to(home)
            (OUT / rel).parent.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(db)
            dst = sqlite3.connect(OUT / rel)
            src.backup(dst)
            src.close()
            dst.execute("PRAGMA journal_mode = DELETE")
            dst.execute("VACUUM")
            dst.close()
    for path in sorted(OUT.rglob("*")):
        if path.is_file():
            print(path.relative_to(OUT), path.stat().st_size)


if __name__ == "__main__":
    main()
