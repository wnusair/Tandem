import sys
from pathlib import Path

# The server isn't an installed package, so "app" is only importable with
# server/ on sys.path, the way run.py expects. conftest loads before any test
# module, so it's the one place this reliably happens first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
