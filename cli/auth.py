"""
Tandem CLI authentication module — JWT + OS keyring edition.

Security model:
  - Credentials are NEVER stored on disk. Passwords are only held in memory
    for the duration of the login request.
  - Access tokens (15-min TTL) and refresh tokens (7-day TTL) are stored in
    the OS-native secure keyring (Keychain on macOS, libsecret/GNOME Keyring
    on Linux, Windows Credential Manager on Windows).
  - If the keyring is unavailable (headless CI), tokens fall back to a
    chmod-600 file at ~/.tandem/credentials.json (never in the project dir).
  - The API key is also stored in the keyring, replacing the old .env approach.
  - Token refresh is handled transparently: if an access token is near expiry,
    the CLI will automatically call /api/v1/auth/refresh before retrying.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "https://tandem.wnusair.org"
_REQUEST_TIMEOUT_SECONDS = 15
_KEYRING_SERVICE = "tandem-cli"
_KEYRING_USERNAME_KEY = "tandem_username"
_KEYRING_ACCESS_TOKEN_KEY = "tandem_access_token"
_KEYRING_REFRESH_TOKEN_KEY = "tandem_refresh_token"
_KEYRING_API_KEY_KEY = "tandem_api_key"
_KEYRING_SERVER_URL_KEY = "tandem_server_url"
_KEYRING_REGISTRATION_TOKEN_KEY = "tandem_node_registration_token"
_FALLBACK_CREDS_PATH = Path.home() / ".tandem" / "credentials.json"


# ---------------------------------------------------------------------------
# Keyring abstraction (with secure-file fallback)
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


def _keyring_set(key: str, value: str) -> None:
    # keyring may import fine but have no working backend (e.g. a headless server
    # or container). In that case fall back to the secure file instead of crashing.
    if _keyring_available():
        import keyring

        try:
            keyring.set_password(_KEYRING_SERVICE, key, value)
            return
        except Exception:
            pass
    _file_set(key, value)


def _keyring_get(key: str) -> str | None:
    if _keyring_available():
        import keyring

        try:
            value = keyring.get_password(_KEYRING_SERVICE, key)
            if value is not None:
                return value
        except Exception:
            pass
    return _file_get(key)


def _keyring_delete(key: str) -> None:
    if _keyring_available():
        import keyring

        try:
            keyring.delete_password(_KEYRING_SERVICE, key)
        except Exception:
            pass
    _file_delete(key)


def _file_set(key: str, value: str) -> None:
    _FALLBACK_CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if _FALLBACK_CREDS_PATH.exists():
        try:
            data = json.loads(_FALLBACK_CREDS_PATH.read_text())
        except Exception:
            data = {}
    data[key] = value
    _FALLBACK_CREDS_PATH.write_text(json.dumps(data))
    if os.name == "posix":
        os.chmod(_FALLBACK_CREDS_PATH, stat.S_IRUSR | stat.S_IWUSR)


def _file_get(key: str) -> str | None:
    if not _FALLBACK_CREDS_PATH.exists():
        return None
    try:
        data = json.loads(_FALLBACK_CREDS_PATH.read_text())
        return data.get(key)
    except Exception:
        return None


def _file_delete(key: str) -> None:
    if not _FALLBACK_CREDS_PATH.exists():
        return
    try:
        data = json.loads(_FALLBACK_CREDS_PATH.read_text())
        data.pop(key, None)
        _FALLBACK_CREDS_PATH.write_text(json.dumps(data))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session data class
# ---------------------------------------------------------------------------

@dataclass
class AuthSession:
    username: str
    access_token: str
    refresh_token: str
    api_key: str
    server_url: str


# ---------------------------------------------------------------------------
# Server URL resolution
# ---------------------------------------------------------------------------

def resolve_server_url(server_url: str | None = None) -> str:
    resolved = (
        server_url
        or _keyring_get(_KEYRING_SERVER_URL_KEY)
        or os.environ.get("TANDEM_SERVER_URL")
        or os.environ.get("SERVER_URL")
        or _DEFAULT_SERVER_URL
    )
    return resolved.rstrip("/")


def get_stored_server_url() -> str | None:
    """Return the server URL saved via `tandem settings set-server-url`, if any."""
    return _keyring_get(_KEYRING_SERVER_URL_KEY)


def set_stored_server_url(server_url: str) -> str:
    """Save a server URL so every command uses it without needing --server-url each time."""
    normalized = server_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("Server URL cannot be empty.")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("Server URL must start with http:// or https://")
    _keyring_set(_KEYRING_SERVER_URL_KEY, normalized)
    return normalized


def clear_stored_server_url() -> None:
    """Remove the saved server URL, falling back to TANDEM_SERVER_URL/SERVER_URL or the default."""
    _keyring_delete(_KEYRING_SERVER_URL_KEY)


# ---------------------------------------------------------------------------
# Node registration token
# ---------------------------------------------------------------------------
# Some servers require a shared secret before they'll let a new machine
# register as a node (see TANDEM_NODE_REGISTRATION_TOKEN on the server side).
# We save it the same way we save the server URL, so it survives across
# terminal sessions instead of needing to be exported by hand every time.

def get_stored_registration_token() -> str | None:
    """Return the token saved via `tandem settings set-registration-token`, if any."""
    return _keyring_get(_KEYRING_REGISTRATION_TOKEN_KEY)


def set_stored_registration_token(registration_token: str) -> str:
    """Save a node registration token so `tandem node start` sends it automatically."""
    normalized = registration_token.strip()
    if not normalized:
        raise ValueError("Registration token cannot be empty.")
    _keyring_set(_KEYRING_REGISTRATION_TOKEN_KEY, normalized)
    return normalized


def clear_stored_registration_token() -> None:
    """Remove the saved registration token, falling back to TANDEM_NODE_REGISTRATION_TOKEN if set."""
    _keyring_delete(_KEYRING_REGISTRATION_TOKEN_KEY)


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

def prompt_username(username: str | None) -> str:
    resolved = (username or "").strip()
    if resolved:
        return resolved
    try:
        resolved = input("Username: ").strip()
    except EOFError as exc:
        raise ValueError(
            "Could not read a username from stdin. Pass --username or run in an interactive terminal."
        ) from exc
    if not resolved:
        raise ValueError("Username is required.")
    return resolved


def prompt_password(password: str | None, *, confirm: bool = False) -> str:
    if password is not None:
        if not password:
            raise ValueError("Password is required.")
        return password
    try:
        resolved = getpass.getpass("Password: ")
    except EOFError as exc:
        raise ValueError(
            "Could not read a password from stdin. Pass --password or run in an interactive terminal."
        ) from exc
    if not resolved:
        raise ValueError("Password is required.")
    if confirm:
        confirmation = getpass.getpass("Confirm password: ")
        if resolved != confirmation:
            raise ValueError("Passwords did not match.")
    return resolved


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _response_payload(response: requests.Response) -> Any:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "application/json" in content_type:
        try:
            return response.json()
        except ValueError:
            return response.text.strip()
    return response.text.strip()


def _raise_response_error(response: requests.Response) -> None:
    payload = _response_payload(response)
    if isinstance(payload, dict):
        detail = (
            payload.get("error")
            or payload.get("message")
            or payload.get("detail")
            or str(payload)
        )
    else:
        detail = str(payload) or "request failed"
    raise RuntimeError(
        f"{response.request.method} {response.url} failed with {response.status_code}: {detail}"
    )


# ---------------------------------------------------------------------------
# Core auth operations
# ---------------------------------------------------------------------------

def register_user(
    *,
    username: str,
    password: str,
    server_url: str | None = None,
) -> None:
    """Register a new user account on the Tandem server."""
    resolved_server_url = resolve_server_url(server_url)
    response = requests.post(
        f"{resolved_server_url}/api/v1/auth/register",
        json={"username": username, "password": password},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 201:
        _raise_response_error(response)


def login_user(
    *,
    username: str,
    password: str,
    server_url: str | None = None,
    rotate_api_key: bool = False,
) -> AuthSession:
    """Authenticate and return a session with JWT tokens and API key."""
    resolved_server_url = resolve_server_url(server_url)
    response = requests.post(
        f"{resolved_server_url}/api/v1/auth/login",
        json={
            "username": username,
            "password": password,
            "rotate_api_key": rotate_api_key,
        },
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        _raise_response_error(response)
    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Login response was not valid JSON.")
    for field in ("access_token", "refresh_token", "username", "api_key"):
        if not payload.get(field):
            raise RuntimeError(f"Server response was missing `{field}`.")
    return AuthSession(
        username=payload["username"],
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        api_key=payload["api_key"],
        server_url=resolved_server_url,
    )


def refresh_access_token(server_url: str | None = None) -> str | None:
    """Use the stored refresh token to obtain a new access token."""
    refresh_token = _keyring_get(_KEYRING_REFRESH_TOKEN_KEY)
    if not refresh_token:
        return None
    resolved_server_url = resolve_server_url(server_url)
    reason: str | None = None
    try:
        response = requests.post(
            f"{resolved_server_url}/api/v1/auth/refresh",
            json={"refresh_token": refresh_token},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            data = response.json()
            new_access_token = data.get("access_token")
            if new_access_token:
                _keyring_set(_KEYRING_ACCESS_TOKEN_KEY, new_access_token)
                return new_access_token
            reason = "the server's response was missing an access token"
        else:
            reason = f"the server returned {response.status_code}"
    except Exception as exc:
        reason = str(exc)

    print(
        f"warning: Could not refresh your session ({reason}). "
        "If commands start failing with auth errors, run `tandem auth login` again.",
        file=sys.stderr,
    )
    return None


def store_auth_session(session: AuthSession) -> None:
    """Persist all session tokens and credentials to the OS keyring."""
    _keyring_set(_KEYRING_SERVER_URL_KEY, session.server_url)
    _keyring_set(_KEYRING_USERNAME_KEY, session.username)
    _keyring_set(_KEYRING_ACCESS_TOKEN_KEY, session.access_token)
    _keyring_set(_KEYRING_REFRESH_TOKEN_KEY, session.refresh_token)
    _keyring_set(_KEYRING_API_KEY_KEY, session.api_key)


def load_auth_session() -> AuthSession | None:
    """Load the stored session from the OS keyring."""
    username = _keyring_get(_KEYRING_USERNAME_KEY)
    access_token = _keyring_get(_KEYRING_ACCESS_TOKEN_KEY)
    refresh_token = _keyring_get(_KEYRING_REFRESH_TOKEN_KEY)
    api_key = _keyring_get(_KEYRING_API_KEY_KEY)
    server_url = _keyring_get(_KEYRING_SERVER_URL_KEY) or _DEFAULT_SERVER_URL
    if not all([username, access_token, refresh_token, api_key]):
        return None
    return AuthSession(
        username=username,
        access_token=access_token,
        refresh_token=refresh_token,
        api_key=api_key,
        server_url=server_url,
    )


def clear_auth_session(server_url: str | None = None) -> None:
    """Log out: revoke the refresh token on the server and clear local credentials."""
    refresh_token = _keyring_get(_KEYRING_REFRESH_TOKEN_KEY)
    if refresh_token:
        resolved_server_url = resolve_server_url(server_url)
        try:
            requests.post(
                f"{resolved_server_url}/api/v1/auth/logout",
                json={"refresh_token": refresh_token},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            pass
    for key in [
        _KEYRING_USERNAME_KEY,
        _KEYRING_ACCESS_TOKEN_KEY,
        _KEYRING_REFRESH_TOKEN_KEY,
        _KEYRING_API_KEY_KEY,
        _KEYRING_SERVER_URL_KEY,
        _KEYRING_REGISTRATION_TOKEN_KEY,
    ]:
        _keyring_delete(key)


def get_access_token(server_url: str | None = None) -> str | None:
    """Return a valid access token, refreshing transparently if near expiry."""
    token = _keyring_get(_KEYRING_ACCESS_TOKEN_KEY)
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) == 3:
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp", 0)
            if exp - time.time() < 60:
                refreshed = refresh_access_token(server_url)
                return refreshed or token
    except Exception:
        pass
    return token


def get_api_key() -> str | None:
    """Return the stored API key for use with deploy/start/stop commands."""
    return _keyring_get(_KEYRING_API_KEY_KEY)


def require_auth(server_url: str | None = None) -> AuthSession:
    """Return the current session or raise RuntimeError if not logged in."""
    session = load_auth_session()
    if session is None:
        raise RuntimeError("Not logged in. Run `tandem auth login` first.")
    fresh_token = get_access_token(server_url or session.server_url)
    if fresh_token:
        session.access_token = fresh_token
    return session
