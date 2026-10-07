#!/usr/bin/env python3
"""Package a built `ragmonk` binary as a native release archive (RUST-15).

    package_release.py --binary target/release/ragmonk --version 1.2.0 \
        --target x86_64-unknown-linux-gnu [--models DIR] --out dist/

Writes `ragmonk-<ver>-<target>.tar.gz` (`.zip` for Windows targets) with a
single top-level directory holding the binary, the bundled models under
`models/`, README.md and LICENSE, and adds or replaces its line in
`<out>/SHA256SUMS`. Entries are sorted with fixed mtimes, so the same
inputs give the same archive. Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import os
import tarfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MTIME = 1767225600  # 2026-01-01T00:00:00Z


def entries(binary: Path, models: Path | None, windows: bool) -> list[tuple[str, Path, int]]:
    name = "ragmonk.exe" if windows else "ragmonk"
    out = [(name, binary, 0o755)]
    for doc in ("README.md", "LICENSE"):
        if (REPO_ROOT / doc).is_file():
            out.append((doc, REPO_ROOT / doc, 0o644))
    if models is not None:
        for path in sorted(p for p in models.rglob("*") if p.is_file()):
            out.append((f"models/{path.relative_to(models).as_posix()}", path, 0o644))
    return out


def build(args: argparse.Namespace) -> Path:
    windows = "windows" in args.target
    top = f"ragmonk-{args.version}-{args.target}"
    archive = args.out / f"{top}.{'zip' if windows else 'tar.gz'}"
    files = entries(args.binary, args.models, windows)
    args.out.mkdir(parents=True, exist_ok=True)
    if windows:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, src, mode in files:
                info = zipfile.ZipInfo(f"{top}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, src.read_bytes())
    else:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
            for rel, src, mode in files:
                info = tarfile.TarInfo(f"{top}/{rel}")
                data = src.read_bytes()
                info.size, info.mode, info.mtime = len(data), mode, MTIME
                t.addfile(info, io.BytesIO(data))
        with (
            open(archive, "wb") as fh,
            gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=MTIME) as gz,
        ):
            gz.write(buf.getvalue())
    return archive


def update_sums(out: Path, archive: Path) -> None:
    sums = out / "SHA256SUMS"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    lines = [
        line
        for line in (sums.read_text().splitlines() if sums.is_file() else [])
        if line.split()[-1].lstrip("*") != archive.name
    ]
    lines.append(f"{digest}  {archive.name}")
    sums.write_text("\n".join(sorted(lines, key=lambda line: line.split()[-1])) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--models", type=Path)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.version = args.version.removeprefix("v")
    archive = build(args)
    update_sums(args.out, archive)
    print(archive)


if __name__ == "__main__":
    os.umask(0o022)
    main()
