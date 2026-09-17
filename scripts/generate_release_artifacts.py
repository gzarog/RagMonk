#!/usr/bin/env python3
"""Builds RagMonk's sdist/wheel, then generates a checksum file and a
basic dependency manifest ("SBOM") alongside them.

Scope (Phase 8, priority 4 -- see CHANGELOG.md): this produces *unsigned*
release artifacts. No code-signing certificate or secret exists in this
environment/repository, so signing is deliberately not attempted here
(there is nothing to fake it with, and a stub signature would be worse
than none -- it would look signed without being verifiable). A real
signing step (e.g. Sigstore/cosign, or a vendor certificate) is future
work once that infrastructure exists.

The dependency manifest is a plain, hand-rolled JSON document (name,
version, license per installed distribution) -- not output from a
dedicated SBOM tool (e.g. CycloneDX, SPDX) or `pip-audit`, neither of
which is currently a project dependency. This is an intentionally modest
substitute: honest and inspectable, not a claim of standards compliance.

Usage:
    python scripts/generate_release_artifacts.py [--dist-dir dist]

Requires the ``build`` package (``pip install build``) to build the
sdist/wheel, and the project's own runtime dependencies to already be
installed (so their versions can be read via ``importlib.metadata``) --
exactly the state a normal ``pip install -e ".[dev]"`` dev environment,
or the release CI workflow, is already in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_build(dist_dir: Path) -> None:
    dist_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(dist_dir), str(REPO_ROOT)],
        check=True,
    )


def _sha256sums(dist_dir: Path) -> Path:
    artifacts = sorted(p for p in dist_dir.glob("*") if p.suffix in (".whl", ".gz"))
    if not artifacts:
        raise SystemExit(f"no build artifacts found in {dist_dir}; did the build step run?")

    lines = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")

    checksums_path = dist_dir / "SHA256SUMS"
    checksums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksums_path


def _project_dependency_names() -> list[str]:
    """Top-level dependency names declared in ``pyproject.toml`` -- read
    directly from the file rather than the installed distribution's own
    metadata, so this reflects the source of truth developers edit.
    """
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw_deps: list[str] = data["project"]["dependencies"]
    names = []
    for spec in raw_deps:
        # A minimal PEP 508 name extraction (name stops at the first
        # version/marker/extras delimiter) -- good enough for the small,
        # simple dependency list this project declares; not a general
        # PEP 508 parser.
        name = spec
        for delim in ("[", "=", "<", ">", "!", "~", ";", " "):
            name = name.split(delim, 1)[0]
        names.append(name.strip())
    return names


def _component_for(name: str) -> dict[str, str]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return {"name": name, "version": "unknown", "license": "unknown"}
    license_value = dist.metadata.get("License") or "unknown"
    # Modern packaging increasingly reports license via an SPDX classifier
    # instead of the free-text ``License`` field -- fall back to that when
    # present, since "unknown" would otherwise be misleadingly common.
    if license_value in ("unknown", "UNKNOWN", ""):
        for classifier in dist.metadata.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                license_value = classifier.rsplit("::", 1)[-1].strip()
                break
    return {"name": name, "version": dist.version, "license": license_value}


def _generate_sbom(dist_dir: Path) -> Path:
    components = [_component_for(name) for name in sorted(_project_dependency_names())]
    manifest = {
        "format": "ragmonk-dependency-manifest",
        "format_note": (
            "Not CycloneDX/SPDX -- a plain, hand-rolled dependency listing. "
            "See scripts/generate_release_artifacts.py."
        ),
        "ragmonk_version": metadata.version("ragmonk"),
        "generated_at": datetime.now(UTC).isoformat(),
        "python_version": sys.version.split()[0],
        "components": components,
    }
    sbom_path = dist_dir / "sbom.json"
    sbom_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return sbom_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist", help="Output directory (default: dist/)")
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip the sdist/wheel build step and only (re)generate checksums/SBOM "
        "for artifacts already present in --dist-dir.",
    )
    args = parser.parse_args()

    dist_dir = (REPO_ROOT / args.dist_dir).resolve()
    if not args.skip_build:
        _run_build(dist_dir)

    checksums_path = _sha256sums(dist_dir)
    sbom_path = _generate_sbom(dist_dir)

    print(f"Wrote {checksums_path}")
    print(f"Wrote {sbom_path}")
    print("NOTE: these artifacts are UNSIGNED -- no code-signing infrastructure exists here.")


if __name__ == "__main__":
    main()
