#!/bin/sh
# Installs the RAGpilot CLI: downloads the source for $RAGPILOT_REF (default:
# main), creates an isolated virtual environment, installs the package into
# it, and links the `ragpilot` executable onto a per-user bin directory.
#
# Requires Python 3.12+ already on PATH -- this script does not install
# Python itself. See README.md's Installation section for details.
set -eu

REPO="gzarog/Ragpilotv2"
REF="${RAGPILOT_REF:-main}"
INSTALL_DIR="${RAGPILOT_INSTALL_DIR:-$HOME/.ragpilot}"
APP_DIR="$INSTALL_DIR/app"
VENV_DIR="$INSTALL_DIR/venv"
BIN_DIR="${RAGPILOT_BIN_DIR:-$HOME/.local/bin}"
# Same default as ragpilot's own core/paths.py::runtime_dir() on POSIX --
# RAGPILOT_INSTALL_DIR/RAGPILOT_HOME happen to share a default today, but
# are independent overrides, so this is computed the same way rather than
# assumed equal to INSTALL_DIR above.
RAGPILOT_HOME="${RAGPILOT_HOME:-$HOME/.ragpilot}"

find_python() {
    for candidate in python3.13 python3.12 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
            major="${version%%.*}"
            minor="${version#*.}"
            if [ "$major" -eq 3 ] && [ "$minor" -ge 12 ] 2>/dev/null; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python)" || {
    echo "error: RAGpilot requires Python 3.12+, but no suitable interpreter was found on PATH." >&2
    echo "Install Python 3.12 or newer (https://www.python.org/downloads/) and re-run this script." >&2
    exit 1
}
echo "Using $("$PYTHON" --version) at $(command -v "$PYTHON")"

TARBALL="$(mktemp)"
trap 'rm -f "$TARBALL"' EXIT
echo "Downloading RAGpilot ($REF)..."
DOWNLOAD_URL="https://github.com/$REPO/archive/refs/heads/$REF.tar.gz"
# GitHub's archive/codeload endpoint can briefly 404 a branch that was just
# pushed (its tarball cache lags the push by a few seconds) -- retry a
# handful of times with linear backoff before giving up, rather than
# failing outright on what is usually a transient race.
attempt=1
max_attempts=5
until curl -fsSL "$DOWNLOAD_URL" -o "$TARBALL"; do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "error: failed to download $DOWNLOAD_URL after $attempt attempts" >&2
        exit 1
    fi
    echo "Download attempt $attempt failed, retrying in ${attempt}s..." >&2
    sleep "$attempt"
    attempt=$((attempt + 1))
done

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR"
tar -xzf "$TARBALL" -C "$APP_DIR" --strip-components=1

# Resolve a version to report via SETUPTOOLS_SCM_PRETEND_VERSION: the
# downloaded tarball above has no .git for hatch-vcs to derive one from,
# so it would otherwise always fall back to the hardcoded "0.0.0"
# placeholder (see pyproject.toml's [tool.hatch.version]
# fallback-version). $REF is used directly when it already looks like
# this project's own release-tag shape (an upgrade -- update/installer.py
# always passes an exact, already-validated tag here); the default
# "main" instead queries GitHub for the latest actual release, since
# "main" itself isn't a version. Any other custom/branch $REF is left
# unresolved -- reporting an unrelated release's version for arbitrary
# branch content would be actively misleading. A failed or missing
# lookup (offline, no releases yet) just skips the override, same as
# before this existed.
PRETEND_VERSION=""
case "$REF" in
    v[0-9]*.[0-9]*.[0-9]*)
        PRETEND_VERSION="${REF#v}"
        ;;
    main)
        LATEST_TAG="$(curl -fsSL -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
            | grep -o '"tag_name" *: *"[^"]*"' | head -n1 | sed 's/.*"\([^"]*\)"$/\1/')"
        case "$LATEST_TAG" in
            v[0-9]*.[0-9]*.[0-9]*) PRETEND_VERSION="${LATEST_TAG#v}" ;;
        esac
        ;;
esac

echo "Creating virtual environment at $VENV_DIR..."
rm -rf "$VENV_DIR"
"$PYTHON" -m venv "$VENV_DIR"

echo "Installing RAGpilot (this downloads its dependencies, including torch -- may take a few minutes)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
# Purge pip's cache before the real install: an entry written by whatever
# pip version was previously on this machine can fail to deserialize under
# the version just upgraded to above ("WARNING: Cache entry deserialization
# failed, entry ignored") -- pip already degrades safely from that (just
# re-downloads), but starting from a clean cache means it shouldn't happen
# at all. `|| true`: a cache that doesn't exist yet, or isn't writable, is
# not a reason to abort the install.
"$VENV_DIR/bin/pip" cache purge >/dev/null 2>&1 || true
# Still explained below in case some other/newer cache mismatch shows up
# despite the purge above: harmless either way, pip just re-downloads that
# entry instead of using a stale cache.
echo "(you may see \"Cache entry deserialization failed\" warnings below -- harmless, pip just re-downloads that entry)"
# Deliberately not --quiet here: pip's normal download/build progress output
# is the only feedback during a multi-minute, multi-hundred-MB install (torch
# chief among the dependencies) -- silencing it makes a slow-but-working
# install indistinguishable from a hung one.
if [ -n "$PRETEND_VERSION" ]; then
    SETUPTOOLS_SCM_PRETEND_VERSION="$PRETEND_VERSION" "$VENV_DIR/bin/pip" install "$APP_DIR"
else
    "$VENV_DIR/bin/pip" install "$APP_DIR"
fi

mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/ragpilot" "$BIN_DIR/ragpilot"
echo "RAGpilot installed: $BIN_DIR/ragpilot"

# Lets `ragpilot update install` (update/installer.py) detect that this is
# an install-script install and where to re-run this same script, rather
# than guessing from the running interpreter's own path -- see this
# file's own record of itself as the one thing that can't guess itself.
mkdir -p "$RAGPILOT_HOME"
cat > "$RAGPILOT_HOME/install_info.json" <<EOF
{
  "install_method": "install-script",
  "repository": "$REPO",
  "install_dir": "$INSTALL_DIR",
  "venv_dir": "$VENV_DIR",
  "bin_dir": "$BIN_DIR"
}
EOF

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo ""
        echo "warning: $BIN_DIR is not on your PATH."
        echo "Add this to your shell profile (~/.bashrc, ~/.zshrc, ~/.profile, ...):"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

echo ""
echo "Run 'ragpilot version' to verify, then 'ragpilot init' to get started."
