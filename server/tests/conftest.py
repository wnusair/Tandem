import sys
from pathlib import Path

# The server isn't installed as a package (no setup.py/pyproject.toml under
# server/), so "app" is only importable once server/ itself is on sys.path --
# exactly how server/run.py expects to be run. conftest.py loads before any
# test module in this directory, which is why this lives here rather than as
# a side effect inside whichever test file happens to be collected first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
