from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tandem_cli import commands


class UsageCommandTests(unittest.TestCase):
    def _run_with_payload(self, payload: dict) -> str:
        args = argparse.Namespace(server_url=None, api_key=None)
        buffer = io.StringIO()
        with patch("tandem_cli.commands.fetch_usage", return_value=payload):
            with redirect_stdout(buffer):
                exit_code = commands._cmd_usage(args)
        self.assertEqual(exit_code, 0)
        return buffer.getvalue()

    def test_renders_measured_and_placeholder_metrics(self) -> None:
        payload = {
            "account": {"user_id": 1},
            "resources": [
                {
                    "type": "compute",
                    "used": 250,
                    "limit": 1000,
                    "unit": "seconds",
                    "percent": 25.0,
                    "source": "measured",
                },
                {
                    "type": "ram",
                    "used": 0,
                    "limit": 5 * 2**30,
                    "unit": "bytes",
                    "percent": 0.0,
                    "source": "placeholder",
                },
            ],
        }

        output = self._run_with_payload(payload)

        self.assertIn("compute", output)
        self.assertIn("25.0%", output)
        self.assertIn("seconds", output)
        # the placeholder metric is labelled and rendered in GiB
        self.assertIn("ram", output)
        self.assertIn("(placeholder)", output)
        self.assertIn("GiB", output)

    def test_parser_accepts_the_usage_command(self) -> None:
        parser = commands._build_parser()
        parsed = parser.parse_args(["usage"])
        self.assertEqual(parsed.command, "usage")


if __name__ == "__main__":
    unittest.main()
