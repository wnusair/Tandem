#!/usr/bin/env bash
#
# Build the macOS disk image (.dmg) for Tandem.
#
#   packaging/build-dmg.sh
#
# Produces packaging/dist/tandem-macos-<arch>.dmg. The image holds the tandem-cli
# folder and the tandem-node binary, plus a double-clickable Install.command that
# puts tandem-node straight on /usr/local/bin and symlinks tandem there too (the
# CLI itself lives in /usr/local/lib/tandem-cli -- see build-binary.sh for why
# it's a folder, not a single file). So opening the image and running the
# installer gives a Mac user the same thing the .deb gives a Linux user: the
# `tandem` command and the node it drives, ready to go.
#
# This one only runs on macOS -- a .dmg is made with hdiutil, which doesn't exist
# on Linux or Windows. On those, use build-deb.sh or the Windows steps instead.

set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  echo "build-dmg.sh only runs on macOS (it needs hdiutil)."
  echo "On Linux build a .deb with build-deb.sh; on Windows use install.bat."
  exit 1
fi

# Where things are. This script lives in packaging/, so the repo root is one up.
PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$PACKAGING_DIR/.." && pwd)"
DIST_DIR="$PACKAGING_DIR/dist"

NODE_DIR="$REPO_ROOT/node"
CLI_DIR="$REPO_ROOT/cli"

# The version is the CLI's version -- same source of truth as build-deb.sh.
VERSION="$(grep -m1 '^version' "$CLI_DIR/pyproject.toml" | cut -d'"' -f2)"
ARCH="$(uname -m)"  # arm64 on Apple Silicon, x86_64 on Intel

# 1. Make sure both binaries exist, building each if needed (same as build-deb.sh).
NODE_BINARY="$NODE_DIR/target/release/tandem-node"
if [ ! -f "$NODE_BINARY" ]; then
  echo "Building the node release binary first..."
  cargo build --release --manifest-path "$NODE_DIR/Cargo.toml"
fi

CLI_BUNDLE_DIR="$CLI_DIR/packaging/dist/tandem"
if [ ! -d "$CLI_BUNDLE_DIR" ]; then
  echo "Building the CLI binary first..."
  bash "$CLI_DIR/packaging/build-binary.sh"
fi

echo "Packaging tandem $VERSION ($ARCH) into a .dmg..."

# 2. Everything that should show up when the user opens the disk image.
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

# The CLI ships as a folder, not a single file (see build-binary.sh for why
# it's --onedir) -- copy the whole thing so Install.command can put it
# somewhere stable and symlink just the executable onto the PATH.
cp -R "$CLI_BUNDLE_DIR" "$STAGING/tandem-cli"
install -m 0755 "$NODE_BINARY" "$STAGING/tandem-node"

# A friendly installer the user can double-click from the mounted image. It puts
# both commands on the PATH.
cat > "$STAGING/Install.command" <<'INSTALL'
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "Installing tandem and tandem-node (you may be asked for your password)..."
sudo mkdir -p /usr/local/bin /usr/local/lib
sudo rm -rf /usr/local/lib/tandem-cli
sudo cp -R "$HERE/tandem-cli" /usr/local/lib/tandem-cli
sudo ln -sf /usr/local/lib/tandem-cli/tandem /usr/local/bin/tandem
sudo install -m 0755 "$HERE/tandem-node" /usr/local/bin/tandem-node
echo "Done. Log in with 'tandem auth login', then start the worker with 'tandem node start'."
INSTALL
chmod 0755 "$STAGING/Install.command"

cat > "$STAGING/README.txt" <<'README'
Tandem
======

This disk image contains both Tandem commands:
  * tandem-cli/   the command-line tool (a folder, not a single file --
                   PyInstaller needs its support files alongside the binary)
  * tandem-node   the compute node it drives

To install:
  * Double-click Install.command, OR
  * do it yourself:
      sudo cp -R tandem-cli /usr/local/lib/tandem-cli
      sudo ln -sf /usr/local/lib/tandem-cli/tandem /usr/local/bin/tandem
      sudo install -m 0755 tandem-node /usr/local/bin/tandem-node

Once they're on your PATH:
  tandem auth login      # log in
  tandem node start      # start the worker in the background
  tandem status          # check login + node
README

mkdir -p "$DIST_DIR"
OUTPUT="$DIST_DIR/tandem-macos-${ARCH}.dmg"
rm -f "$OUTPUT"

hdiutil create \
  -volname "Tandem" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  "$OUTPUT" >/dev/null

echo "Built $OUTPUT"

# 3. Also install onto this machine right now, same as double-clicking
# Install.command would do. That way running this script is enough on its
# own -- the .dmg is just there to hand off to someone else's Mac.
echo "Installing tandem and tandem-node (you may be asked for your password)..."
sudo mkdir -p /usr/local/bin /usr/local/lib
sudo rm -rf /usr/local/lib/tandem-cli
sudo cp -R "$STAGING/tandem-cli" /usr/local/lib/tandem-cli
sudo ln -sf /usr/local/lib/tandem-cli/tandem /usr/local/bin/tandem
sudo install -m 0755 "$STAGING/tandem-node" /usr/local/bin/tandem-node
echo "Installed. Log in with 'tandem auth login', then start the worker with 'tandem node start'."
