from __future__ import annotations

import unittest
from pathlib import Path

# Files that should be byte-for-byte identical between the canonical SDK
# source and the copy bundled inside the CLI (see cli/sdk_commands.py's
# _LOCAL_SDK_BUNDLES). We keep two physical copies on purpose -- setuptools
# can't package files from outside the cli/ directory -- so this test is the
# safety net that catches the two copies drifting apart.
_TRACKED_FILES = (
    "pyproject.toml",
    "README.md",
    "tandem/__init__.py",
    "tandem/compute.py",
    "tandem/discovery.py",
    "tandem/errors.py",
    "tandem/future.py",
    "tandem/immutable.py",
    "tandem/rpc.py",
    "tandem/split.py",
    "tandem/validator.py",
)


class SdkBundleSyncTests(unittest.TestCase):
    def test_bundled_python_sdk_matches_the_canonical_source(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        source_dir = repo_root / "sdk" / "python-sdk"
        bundled_dir = repo_root / "cli" / "_bundled" / "sdk" / "python_sdk"

        for rel_path in _TRACKED_FILES:
            source_text = (source_dir / rel_path).read_text(encoding="utf-8")
            bundled_text = (bundled_dir / rel_path).read_text(encoding="utf-8")
            self.assertEqual(
                source_text,
                bundled_text,
                f"{rel_path} has drifted between sdk/python-sdk and the CLI's bundled "
                "copy -- re-copy the file from sdk/python-sdk to cli/_bundled/sdk/python_sdk.",
            )


if __name__ == "__main__":
    unittest.main()
