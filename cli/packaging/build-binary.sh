#!/usr/bin/env bash
#
# Turn the Tandem Python CLI into a self-contained executable.
#
#   cli/packaging/build-binary.sh
#
# Produces cli/packaging/dist/tandem/ -- a folder holding the `tandem`
# executable plus everything it needs, with no Python, no virtualenv, and no
# pip install needed on the target machine. The installers (see
# ../../packaging/build-deb.sh and build-dmg.sh) drop the whole folder
# somewhere out of the way and symlink just the executable onto the PATH.
#
# We do this with PyInstaller, which walks the CLI's imports, bundles them plus a
# Python interpreter, and glues it all into one folder for whatever OS you run
# this on (Linux here, macOS on a Mac, Windows on Windows).
#
# This used to build with --onefile (a single executable) instead of --onedir
# (a folder). We switched because of a nasty macOS startup delay: our binary is
# only ad-hoc signed, not notarized, so Gatekeeper does a several-second network
# scan the first time it sees a given file. --onefile re-extracts itself to a
# brand-new temp path on every single launch, so that file is "new" to
# Gatekeeper every time and it re-scans every time, adding 5-10 seconds to every
# `tandem` command forever. --onedir extracts once, to a stable path, so
# Gatekeeper scans it once and remembers -- every launch after the first is
# instant. Same tradeoff doesn't bite on Linux (no Gatekeeper), but there's no
# reason to keep two different packaging modes for one platform, so onedir it
# is everywhere.

set -euo pipefail

# Where things live. This script sits in cli/packaging, so the cli/ package is
# one directory up and the repo root is two up.
PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_DIR="$(cd "$PACKAGING_DIR/.." && pwd)"
DIST_DIR="$PACKAGING_DIR/dist"

# 1. Find a Python new enough to build with (3.10+), the same way install.sh does.
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Could not find Python 3.10 or newer to build the CLI binary."
  echo "Install one and re-run, e.g. 'sudo apt install python3.12' on Debian/Ubuntu."
  exit 1
fi

echo "Building the tandem CLI binary with $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# 2. Do the whole build inside a throwaway virtualenv so we never touch the
# system Python or leave anything behind. Everything below lives under here and
# gets deleted on exit, no matter how the script ends.
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
VENV_DIR="$WORK_DIR/venv"

"$PYTHON_BIN" -m venv "$VENV_DIR"
VENV_PY="$VENV_DIR/bin/python"
"$VENV_PY" -m pip install --upgrade pip --quiet

# 3. Install PyInstaller plus the libraries the CLI actually imports at runtime.
#
# These mirror the runtime dependencies in cli/pyproject.toml, with one
# deliberate exception: componentize-py. The CLI never imports it -- `tandem
# build` shells out to the `tandem-compile` engine, which in turn runs
# `componentize-py` as a separate command (see cli/wasm.py). tandem-compile is
# its own binary (the .deb ships it in /usr/bin), so nothing about the compile
# path belongs baked inside this CLI binary. Leaving componentize-py out keeps
# the binary small. If this list falls out of sync with pyproject.toml, the smoke
# test in step 6 fails the build and tells us.
echo "Installing PyInstaller and the CLI's runtime dependencies..."
"$VENV_PY" -m pip install --quiet \
  pyinstaller \
  requests \
  python-dotenv \
  keyring \
  "tomli>=2.0"

# Install the CLI package itself, but without its dependencies (--no-deps) so pip
# doesn't drag the heavy compile toolchain back in. We handled the deps we want
# above.
"$VENV_PY" -m pip install --quiet --no-deps "$CLI_DIR"

# 4. Give PyInstaller a tiny entry script to start from. It does exactly what the
# installed `tandem` command does: call the CLI's main().
ENTRY="$WORK_DIR/entry_tandem.py"
cat > "$ENTRY" <<'ENTRY_PY'
from tandem_cli.commands import main
import sys

if __name__ == "__main__":
    sys.exit(main())
ENTRY_PY

# 5. Bundle it all into a folder (see the --onedir note up top for why).
#   --collect-all tandem_cli  pulls in every CLI submodule plus data files like
#                             dummy.wasm and the bundled SDK.
#   --collect-all keyring     pulls in keyring and its OS backends, which it
#                             loads dynamically (so PyInstaller can't see them
#                             just by following imports).
echo "Bundling the binary with PyInstaller (this takes a minute)..."
"$VENV_PY" -m PyInstaller \
  --onedir \
  --name tandem \
  --collect-all tandem_cli \
  --collect-all keyring \
  --distpath "$DIST_DIR" \
  --workpath "$WORK_DIR/build" \
  --specpath "$WORK_DIR" \
  --clean --noconfirm \
  "$ENTRY"

# 6. Prove the binary actually runs before we call it done. This catches a whole
# class of packaging mistakes (a missing dependency, a backend that didn't get
# bundled) right here instead of on the user's machine.
BUNDLE_DIR="$DIST_DIR/tandem"
BINARY="$BUNDLE_DIR/tandem"
echo ""
echo "Smoke-testing the binary..."
if ! "$BINARY" -h >/dev/null 2>&1; then
  echo "The built binary failed to run 'tandem -h'. Something didn't bundle correctly."
  exit 1
fi

echo "Built $BUNDLE_DIR"
