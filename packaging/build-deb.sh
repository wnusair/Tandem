#!/usr/bin/env bash
#
# Build the Debian package (.deb) for Tandem.
#
#   packaging/build-deb.sh
#
# Produces packaging/dist/tandem_<version>_<arch>.deb. Installing that one package
# puts all three commands on the user's PATH in /usr/bin:
#
#   tandem          the command-line tool (a bundled Python binary)
#   tandem-node     the compute node it drives (the Rust worker)
#   tandem-compile  the compile engine `tandem build` shells out to (Rust)
#
# So a person can double-click the .deb (or `sudo apt install ./the.deb`) and
# immediately run `tandem -h` -- no Python, no pip, no separate node download.
# This is the packaged twin of install.sh: same end result, but shipped as a
# single file instead of run from a checkout.
#
# Runs anywhere dpkg-deb is available. It leans on smaller builders to produce
# the actual binaries first:
#   - the node and the compile engine come from `cargo build` (see below)
#   - the CLI comes from ../cli/packaging/build-binary.sh

set -euo pipefail

# Where things are. This script lives in packaging/, so the repo root is one up.
PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PACKAGING_DIR/.." && pwd)"
DIST_DIR="$PACKAGING_DIR/dist"

NODE_DIR="$REPO_ROOT/node"
CLI_DIR="$REPO_ROOT/cli"

# You normally need dpkg-deb; on macOS you'd get it from `brew install dpkg`.
if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to build a .deb but wasn't found."
  echo "On Debian/Ubuntu it's part of the base system; on macOS: brew install dpkg"
  exit 1
fi

# The version is the CLI's version from pyproject.toml -- that's the headline
# `tandem` command, so its version names the whole package. Keep it in step with
# node/Cargo.toml when you cut a release.
VERSION="$(grep -m1 '^version' "$CLI_DIR/pyproject.toml" | cut -d'"' -f2)"
if [ -z "$VERSION" ]; then
  echo "Could not read the version from $CLI_DIR/pyproject.toml"
  exit 1
fi

# Debian's own name for this CPU (amd64, arm64, ...). Falls back to amd64 if
# dpkg can't tell us for some reason.
ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"

# 1. Make sure the node binary exists, building it from source if needed.
NODE_BINARY="$NODE_DIR/target/release/tandem-node"
if [ ! -f "$NODE_BINARY" ]; then
  echo "Building the node release binary first..."
  cargo build --release --manifest-path "$NODE_DIR/Cargo.toml"
fi

# 1b. Same for the compile engine `tandem build` shells out to. It's a Rust
# workspace binary, so the build output lands under sdk/target (not sdk/core).
COMPILE_BINARY="$REPO_ROOT/sdk/target/release/tandem-compile"
if [ ! -f "$COMPILE_BINARY" ]; then
  echo "Building the compile engine (tandem-compile) first..."
  cargo build --release --manifest-path "$REPO_ROOT/sdk/core/Cargo.toml" --bin tandem-compile
fi

# 2. Build the CLI binary fresh. Unlike the Rust binaries above -- which only
# change when you actually recompile them -- the CLI's Python changes often, so we
# always rebuild it here. Reusing a leftover executable from a previous run would
# silently ship stale CLI code in the package. Its own script turns the Python
# package into a folder holding the `tandem` executable and its dependencies.
CLI_BUNDLE_DIR="$CLI_DIR/packaging/dist/tandem"
echo "Building the CLI binary..."
bash "$CLI_DIR/packaging/build-binary.sh"

echo "Packaging tandem $VERSION ($ARCH)..."

# 3. Lay out the package tree in a scratch directory, then hand it to dpkg-deb.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/DEBIAN"
mkdir -p "$STAGING/usr/bin"
mkdir -p "$STAGING/usr/lib/tandem-cli"

# The CLI ships as a folder (see build-binary.sh for why it's --onedir, not a
# single file), so it goes under /usr/lib and gets a symlink on the PATH --
# same pattern dpkg itself uses for anything that isn't one lone binary.
cp -R "$CLI_BUNDLE_DIR/." "$STAGING/usr/lib/tandem-cli/"
chmod 0755 "$STAGING/usr/lib/tandem-cli/tandem"
ln -s ../lib/tandem-cli/tandem "$STAGING/usr/bin/tandem"

install -m 0755 "$NODE_BINARY" "$STAGING/usr/bin/tandem-node"
install -m 0755 "$COMPILE_BINARY" "$STAGING/usr/bin/tandem-compile"

# Note: the heredoc delimiter is unquoted so $VERSION and $ARCH expand. That
# means backticks and $(...) in the body would run as commands, so keep them
# out -- plain text and single quotes only.
#
# Replaces/Conflicts/Provides: tandem-node let this package cleanly take over
# from the older node-only 'tandem-node' package. Anyone who installed that one
# first gets it swapped out for this combined package instead of hitting a
# "both packages want /usr/bin/tandem-node" file clash.
cat > "$STAGING/DEBIAN/control" <<CONTROL
Package: tandem
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: Tandem <tandem@wnusair.org>
Replaces: tandem-node
Conflicts: tandem-node
Provides: tandem-node
Description: Tandem CLI, compute node, and compile engine
 Everything you need to run Tandem locally, in one package. The tandem command
 deploys and runs WASM jobs, tandem-compile turns marked Python into WASM
 components for it, and tandem-node is the Rust worker that executes them. After
 installing, log in with 'tandem auth login' and start the worker with
 'tandem node start'.
CONTROL

mkdir -p "$DIST_DIR"
OUTPUT="$DIST_DIR/tandem_${VERSION}_${ARCH}.deb"

# --root-owner-group makes the files inside the package owned by root, so it
# installs cleanly regardless of who built it.
dpkg-deb --build --root-owner-group "$STAGING" "$OUTPUT" >/dev/null

echo "Built $OUTPUT"
