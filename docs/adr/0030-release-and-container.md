# ADR 0030: Native release workflow and container (RUST-15, slice 3)

Status: accepted

## Decisions (user)

- **Workflow:** a new `.github/workflows/rust-release.yml`. The Python
  `release.yml` and `main-release.yml` stay untouched until the cutover at
  RUST-16.
- **Container:** published to GHCR as a multi-arch image
  (`ghcr.io/gzarog/ragmonk`, linux/amd64 and linux/arm64).
- **Signing:** a hook. When the `MINISIGN_SECRET_KEY` secret exists the
  workflow signs `SHA256SUMS`; otherwise it publishes checksums only.
- **Installer CI:** the installer jobs from ADR 0029 move from `ci.yml` to
  `rust.yml`, so they run on every PR to `release/rust-rewrite-v1`.

## Release workflow

The workflow only runs on manual dispatch. Its inputs are `version`
(strict `MAJOR.MINOR.PATCH`), `prerelease` (default true) and `image`
(default true).

It can also be started by pushing a `rust-v<MAJOR.MINOR.PATCH>` tag. That
always publishes a pre-release with the image, and the version comes from
the tag. Manual dispatch only works for workflows on the default branch,
and this one lives on `release/rust-rewrite-v1`. The `rust-v` prefix keeps
the tag push from firing the Python `release.yml` (`v*.*.*`). The GitHub
release itself is still tagged `v<version>`, because that is the tag
`ragmonk update` expects.

1. **validate:** checks the version format and refuses a tag `v<version>`
   that already exists.
2. **build:** builds one archive per target on its own runner:

   | Target | Runner |
   |--------|--------|
   | `x86_64-unknown-linux-gnu` | ubuntu-22.04 (an older glibc, for wider compatibility) |
   | `aarch64-apple-darwin` | macos-latest |
   | `x86_64-apple-darwin` | macos-latest (cross target) |
   | `x86_64-pc-windows-msvc` | windows-latest |

   Each build does the following:
   - fetches the pinned models with `rust/scripts/fetch_models.sh`, using
     the same digests as the crates' manifests;
   - runs `cargo build --release --locked` with `RAGMONK_BUILD_VERSION`,
     `RAGMONK_BUILD_COMMIT` and `RAGMONK_MINISIGN_PUBKEY` (from
     `vars.MINISIGN_PUBLIC_KEY`);
   - smoke-tests `version --json`;
   - packages the archive with `package_release.py --models`.
3. **release:** assembles and publishes the GitHub release:
   - merges the per-target checksums into `SHA256SUMS`;
   - writes `install.sh` and `install.ps1` with the public key filled in;
   - signs `SHA256SUMS` with minisign when the secret is set, then verifies
     the signature against the public variable;
   - runs `gh release create v<version> --target <sha>`.

   A tag pushed with `GITHUB_TOKEN` does not trigger the Python
   `release.yml`.
4. **image / image-manifest:** builds the image natively on amd64 and arm64
   runners and pushes each by digest. Each per-platform image is
   smoke-tested, then the two are joined into a manifest list tagged
   `<version>`, plus `latest` for a full release.

Releases are pre-releases by default. GitHub's "latest release" endpoint,
which both the Python updater and `ragmonk update` follow, skips
pre-releases. So a native release reaches existing users only once a
maintainer publishes it as a full release.

## Container (`rust/Dockerfile`)

- **Build stage:** `rust:1.90.0-slim-bookworm`, matching
  `rust-toolchain.toml`. It builds `ragmonk-cli` with `--locked` and fetches
  the models.
- **Runtime stage:** `debian:bookworm-slim` with `ca-certificates` and
  `tini`, and no Python.
- **User and data:** runs as the non-root user `ragmonk` (uid 10001).
  `RAGMONK_HOME=/data` is a volume.
- **Models:** baked in at `/opt/ragmonk/models`, set through
  `RAGMONK_MODELS_DIR` and `RAGMONK_OCR_MODELS_DIR`.
- **Updates:** `RAGMONK_NO_UPDATE_CHECK=1`, because an image is updated by
  pulling a new tag.
- **Entrypoint:** `ragmonk`, with default arguments `--help`.

Example invocations:
- MCP over stdio: `docker run -i -v ragmonk:/data ghcr.io/gzarog/ragmonk serve --mcp`
- Admin UI: `docker run -p 8765:8765 -v ragmonk:/data ghcr.io/gzarog/ragmonk ui --host 0.0.0.0 --no-browser`

There is no cross-compilation. Multi-arch comes from native runners, so the
Dockerfile stays a plain two-stage build.

## Setting up signing

```sh
minisign -G -p ragmonk.pub -s ragmonk.key
```

Then configure the repository:
- **Repository variable `MINISIGN_PUBLIC_KEY`:** the second line of
  `ragmonk.pub`, which is the base64 key.
- **Secret `MINISIGN_SECRET_KEY`:** the whole of `ragmonk.key`.
- **Secret `MINISIGN_PASSWORD`:** the key's password.

From then on, every binary refuses an update whose `SHA256SUMS` signature
does not verify. The installers check the signature too when `minisign` is
installed.

## CI (`rust.yml`)

- `install-script-tests` (Linux and macOS) and
  `install-script-tests-windows` move here from `ci.yml`, unchanged apart
  from the working directory.
- `container-image` builds the amd64 image and checks that:
  - `version --json` reports the build-arg version;
  - there is no Python in the image;
  - `init` followed by `doctor` succeeds;
  - the models are present.

## Not done here

- Deleting the Python release path, `release.yml` and the Python image. That
  is RUST-16.
- macOS notarization and Windows Authenticode signing. These need
  certificates the project does not have. The minisign hook covers
  integrity.
