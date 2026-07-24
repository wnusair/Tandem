from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tandem_cli import commands
from tandem_cli.auth import load_auth_session


def _use_isolated_credentials_file(test_case: unittest.TestCase, tmpdir: str) -> None:
    """Point the CLI's keyring fallback at a throwaway file so tests never touch
    the real ~/.tandem/credentials.json on the machine running them, and so we
    can read the stored session back to prove it was persisted."""
    fake_path = Path(tmpdir) / "credentials.json"
    patcher_available = patch("tandem_cli.auth._keyring_available", return_value=False)
    patcher_path = patch("tandem_cli.auth._FALLBACK_CREDS_PATH", fake_path)
    patcher_available.start()
    patcher_path.start()
    test_case.addCleanup(patcher_available.stop)
    test_case.addCleanup(patcher_path.stop)


class _FakeResponse:
    def __init__(
        self, *, status_code: int, payload: dict[str, object], url: str
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.url = url
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class AuthCommandTests(unittest.TestCase):
    def test_auth_register_stores_session_in_keyring(self) -> None:
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            previous_cwd = Path.cwd()
            os.chdir(tmpdir)
            self.addCleanup(os.chdir, previous_cwd)
            _use_isolated_credentials_file(self, tmpdir)

            # register hits /api/v1/auth/register (201), then the CLI logs in via
            # /api/v1/auth/login (200) to obtain the JWT tokens + API key.
            responses = [
                _FakeResponse(
                    status_code=201,
                    payload={"status": "success"},
                    url="http://127.0.0.1:6767/api/v1/auth/register",
                ),
                _FakeResponse(
                    status_code=200,
                    payload={
                        "status": "success",
                        "message": "authenticated successfully",
                        "username": "demo",
                        "access_token": "access-token-abc",
                        "refresh_token": "refresh-token-xyz",
                        "api_key": "abcd1234efgh5678",
                        "created_api_key": True,
                    },
                    url="http://127.0.0.1:6767/api/v1/auth/login",
                ),
            ]
            captured_payloads: list[dict[str, object]] = []

            def fake_post(url: str, json: dict[str, object], timeout: int):
                self.assertEqual(timeout, 15)
                response = responses.pop(0)
                self.assertEqual(url, response.url)
                captured_payloads.append(json)
                return response

            with patch("tandem_cli.auth.requests.post", side_effect=fake_post):
                with patch(
                    "tandem_cli.auth.getpass.getpass",
                    side_effect=["demo-pass", "demo-pass"],
                ):
                    with patch("sys.stdout", stdout):
                        exit_code = commands.main(
                            [
                                "auth",
                                "register",
                                "--username",
                                "demo",
                                "--server-url",
                                "http://127.0.0.1:6767",
                            ]
                        )

            self.assertEqual(exit_code, 0)

            # Both endpoints were hit in order with the expected bodies. Register
            # sends no rotate flag; the follow-up login defaults it to False.
            self.assertEqual(
                captured_payloads,
                [
                    {"username": "demo", "password": "demo-pass"},
                    {
                        "username": "demo",
                        "password": "demo-pass",
                        "rotate_api_key": False,
                    },
                ],
            )

            # The session is persisted to the keyring (here, its file fallback),
            # not to a .env file.
            self.assertFalse((Path(tmpdir) / ".env").exists())
            stored = load_auth_session()
            self.assertIsNotNone(stored)
            self.assertEqual(stored.username, "demo")
            self.assertEqual(stored.access_token, "access-token-abc")
            self.assertEqual(stored.refresh_token, "refresh-token-xyz")
            self.assertEqual(stored.api_key, "abcd1234efgh5678")
            self.assertEqual(stored.server_url, "http://127.0.0.1:6767")

            output = stdout.getvalue()
            self.assertIn("Registered user: demo", output)
            self.assertIn("Credentials stored securely in OS keyring.", output)
            # Without --show-api-key the key is masked.
            self.assertIn("abcd...5678", output)
            self.assertNotIn("abcd1234efgh5678", output)

    def test_auth_login_stores_session_and_shows_full_key(self) -> None:
        stdout = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            previous_cwd = Path.cwd()
            os.chdir(tmpdir)
            self.addCleanup(os.chdir, previous_cwd)
            _use_isolated_credentials_file(self, tmpdir)

            captured_payloads: list[dict[str, object]] = []

            def fake_post(url: str, json: dict[str, object], timeout: int):
                self.assertEqual(url, "http://127.0.0.1:6767/api/v1/auth/login")
                self.assertEqual(timeout, 15)
                captured_payloads.append(json)
                return _FakeResponse(
                    status_code=200,
                    payload={
                        "status": "success",
                        "message": "authenticated successfully",
                        "username": "demo",
                        "access_token": "access-token-abc",
                        "refresh_token": "refresh-token-xyz",
                        "api_key": "full-visible-key",
                        "created_api_key": True,
                    },
                    url=url,
                )

            with patch("tandem_cli.auth.requests.post", side_effect=fake_post):
                with patch("tandem_cli.auth.getpass.getpass", return_value="demo-pass"):
                    with patch("sys.stdout", stdout):
                        exit_code = commands.main(
                            [
                                "auth",
                                "login",
                                "--username",
                                "demo",
                                "--server-url",
                                "http://127.0.0.1:6767",
                                "--rotate-api-key",
                                "--show-api-key",
                            ]
                        )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                captured_payloads,
                [
                    {
                        "username": "demo",
                        "password": "demo-pass",
                        "rotate_api_key": True,
                    }
                ],
            )

            # Credentials go to the keyring fallback file, never a project .env.
            self.assertFalse((Path(tmpdir) / ".env").exists())
            stored = load_auth_session()
            self.assertIsNotNone(stored)
            self.assertEqual(stored.username, "demo")
            self.assertEqual(stored.access_token, "access-token-abc")
            self.assertEqual(stored.refresh_token, "refresh-token-xyz")
            self.assertEqual(stored.api_key, "full-visible-key")
            self.assertEqual(stored.server_url, "http://127.0.0.1:6767")

            output = stdout.getvalue()
            self.assertIn("Authenticated user: demo", output)
            self.assertIn("Credentials stored securely in OS keyring.", output)
            # --show-api-key prints the key unmasked.
            self.assertIn("API key: full-visible-key", output)


if __name__ == "__main__":
    unittest.main()
