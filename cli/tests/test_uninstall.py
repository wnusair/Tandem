from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tandem_cli import commands, node_paths, node_service, uninstall


class PerformUninstallTests(unittest.TestCase):
    def _make_fake_root(self, tmp: str) -> Path:
        """A stand-in ~/.tandem with the pieces the installers create."""
        root = Path(tmp) / ".tandem"
        (root / "venv" / "bin").mkdir(parents=True)
        (root / "bin").mkdir(parents=True)
        (root / "node").mkdir(parents=True)
        (root / "credentials.json").write_text("{}")
        return root

    def test_removes_the_tandem_home_and_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_fake_root(tmp)
            posix_launcher = Path(tmp) / "local_bin_tandem"
            posix_launcher.write_text("#!/bin/sh\n")

            with patch.object(node_paths, "NODE_HOME", root / "node"), \
                 patch.object(node_paths, "BIN_DIR", root / "bin"), \
                 patch.object(uninstall, "clear_auth_session"), \
                 patch.object(uninstall, "_posix_launchers", return_value=[posix_launcher]), \
                 patch.object(uninstall, "_windows_launcher", return_value=Path(tmp) / "tandem.bat"), \
                 patch.object(node_service, "active_backend", return_value="daemon"), \
                 patch.object(node_service, "stop_node", return_value=False):
                steps = uninstall.perform_uninstall()

            self.assertFalse(root.exists())
            self.assertFalse(posix_launcher.exists())
            self.assertTrue(any("removed" in step for step in steps))

    def test_defers_removal_when_running_from_the_root(self) -> None:
        # In the real world `tandem` runs from inside ~/.tandem, so we can't delete
        # that folder directly -- it should be scheduled instead.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_fake_root(tmp)
            scheduler = MagicMock()

            with patch.object(node_paths, "NODE_HOME", root / "node"), \
                 patch.object(node_paths, "BIN_DIR", root / "bin"), \
                 patch.object(uninstall, "clear_auth_session"), \
                 patch.object(uninstall, "_posix_launchers", return_value=[Path(tmp) / "nope"]), \
                 patch.object(uninstall, "_windows_launcher", return_value=Path(tmp) / "nope.bat"), \
                 patch.object(uninstall, "_running_from", return_value=True), \
                 patch.object(uninstall, "_schedule_root_removal", scheduler), \
                 patch.object(node_service, "active_backend", return_value="daemon"), \
                 patch.object(node_service, "stop_node", return_value=False):
                steps = uninstall.perform_uninstall()

            scheduler.assert_called_once_with(root)
            self.assertTrue(root.exists())
            self.assertTrue(any("scheduled removal" in step for step in steps))


class UninstallCommandTests(unittest.TestCase):
    def _run(self, code_shown: str, code_typed: str) -> tuple[int, MagicMock, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(commands, "_generate_confirmation_code", return_value=code_shown), \
             patch("builtins.input", return_value=code_typed), \
             patch.object(commands, "perform_uninstall", return_value=["did a thing"]) as fake, \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            exit_code = commands.main(["uninstall"])
        return exit_code, fake, stdout.getvalue()

    def test_wrong_code_aborts_without_removing_anything(self) -> None:
        exit_code, fake, out = self._run("123456", "000000")
        self.assertEqual(exit_code, 1)
        fake.assert_not_called()
        self.assertIn("didn't match", out)

    def test_correct_code_runs_the_uninstall(self) -> None:
        exit_code, fake, out = self._run("123456", "123456")
        self.assertEqual(exit_code, 0)
        fake.assert_called_once()
        self.assertIn("removed", out.lower())


if __name__ == "__main__":
    unittest.main()
