"""
Commands for browsing and fetching Tandem SDKs from the server's registry.

These all require the user to already be logged in (`tandem auth login`) --
the server's /api/v1/desktop/sdks route is JWT-protected. The server is the
source of truth for what SDKs/versions exist; actually handing over the SDK
files is done locally for now (see cli/_bundled/sdk/python_sdk), since there
is no real server-side download endpoint yet. This is separate from
cli/sdk_registry.py, which is an unrelated build-time concept (it tells the
CLI's own build/inspect pipeline where to find the bundled marker SDK).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .auth import AuthSession, require_auth

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback.
    import tomli as tomllib  # type: ignore[no-redef]

_REQUEST_TIMEOUT_SECONDS = 15

_BUNDLED_SDKS_DIR = Path(__file__).resolve().parent / "_bundled" / "sdk"

# Where a CLI-bundled copy of each SDK's source lives. The server still decides
# what exists; this only answers where the local copy of a confirmed name is,
# since there's no server-side download endpoint yet.
_LOCAL_SDK_BUNDLES: dict[str, Path] = {
    "tandem-python-sdk": _BUNDLED_SDKS_DIR / "python_sdk",
}


@dataclass(frozen=True)
class ResolvedSdk:
    """An SDK name/version the server confirmed exists, plus where to get it locally."""

    name: str
    version: str
    bundle_path: Path
    warning: str | None = None


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
        detail = payload.get("error") or payload.get("message") or str(payload)
    else:
        detail = str(payload) or "request failed"
    raise RuntimeError(
        f"{response.request.method} {response.url} failed with {response.status_code}: {detail}"
    )


def fetch_sdk_registry() -> tuple[AuthSession, list[dict[str, Any]]]:
    """Ask the server what SDKs/versions exist. Requires an active login."""
    session = require_auth()
    response = requests.get(
        f"{session.server_url}/api/v1/desktop/sdks",
        headers={"Authorization": f"Bearer {session.access_token}"},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        _raise_response_error(response)
    payload = _response_payload(response)
    sdks = payload.get("sdks") if isinstance(payload, dict) else None
    if not isinstance(sdks, list):
        raise RuntimeError("Server response was missing a `sdks` list.")
    return session, sdks


def _latest_version(sdk: dict[str, Any]) -> dict[str, Any] | None:
    versions = sdk.get("versions")
    if isinstance(versions, list) and versions:
        return versions[-1]
    if sdk.get("version"):  # tolerate an older/flat-shaped server response
        return {"version": sdk["version"], "download_url": sdk.get("download_url")}
    return None


def resolve_sdk_name(sdks: list[dict[str, Any]], name: str | None) -> str:
    """`name` if given; auto-select when there's exactly one SDK on the server;
    otherwise raise, listing the available names so the user knows what to type."""
    names = [s["name"] for s in sdks if s.get("name")]
    if name:
        return name
    if len(names) == 1:
        return names[0]
    if not names:
        raise RuntimeError("The server has no SDKs registered yet.")
    raise RuntimeError(
        "More than one SDK is available, so you need to say which one "
        f"(e.g. `tandem sdk install {names[0]}`). Available: {', '.join(sorted(names))}"
    )


def bundled_version(bundle_path: Path) -> str | None:
    """Read the version out of a bundled SDK's pyproject.toml (no import needed)."""
    pyproject_path = bundle_path / "pyproject.toml"
    if not pyproject_path.exists():
        return None
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    project = data.get("project")
    return project.get("version") if isinstance(project, dict) else None


def resolve_sdk(name: str | None) -> ResolvedSdk:
    """Confirm a name (explicit or auto-selected) against the live server
    registry, then locate the CLI's local bundle for it. Requires login."""
    _session, sdks = fetch_sdk_registry()
    resolved_name = resolve_sdk_name(sdks, name)

    matches = [s for s in sdks if s.get("name") == resolved_name]
    if not matches:
        available = ", ".join(sorted(s.get("name", "?") for s in sdks)) or "(none)"
        raise RuntimeError(f"No SDK named '{resolved_name}' on the server. Available: {available}")
    version_entry = _latest_version(matches[0])
    if version_entry is None:
        raise RuntimeError(f"SDK '{resolved_name}' has no published versions on the server yet.")

    bundle_path = _LOCAL_SDK_BUNDLES.get(resolved_name)
    if bundle_path is None or not bundle_path.exists():
        raise RuntimeError(
            f"The server lists '{resolved_name}', but this CLI build doesn't carry a local "
            "copy of it. Real server-hosted downloads aren't wired up yet, so the CLI can only "
            "hand out SDKs it ships with -- you may need to update the CLI itself."
        )

    server_version = version_entry.get("version")
    local_version = bundled_version(bundle_path)
    warning = None
    if server_version and local_version and server_version != local_version:
        warning = (
            f"the server lists {resolved_name} {server_version}, but this CLI only carries "
            f"{local_version} locally -- you'll get {local_version}."
        )

    return ResolvedSdk(
        name=resolved_name,
        version=local_version or server_version or "unknown",
        bundle_path=bundle_path,
        warning=warning,
    )


def download_sdk(resolved: ResolvedSdk, output_dir: Path) -> Path:
    """Copy the resolved SDK's bundled source into output_dir."""
    if output_dir.exists():
        raise RuntimeError(
            f"{output_dir} already exists. Pass --output to pick a different folder, or remove it first."
        )
    shutil.copytree(resolved.bundle_path, output_dir)
    return output_dir


def resolve_target_python(override: str | None) -> str:
    """Pick the Python environment to install the SDK into.

    tandem may be running from its own isolated install (see install.sh), so
    sys.executable can point at the CLI's private venv instead of whatever
    the user is actually working in. Prefer their activated venv if there is
    one, then whatever `python3`/`python` resolves to on PATH, and only fall
    back to the CLI's own interpreter as a last resort.
    """
    if override:
        return override
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        exe_name = "python.exe" if os.name == "nt" else "python"
        candidate = Path(virtual_env) / bin_dir / exe_name
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or sys.executable


def install_sdk(resolved: ResolvedSdk, *, target_python: str) -> str:
    """Stage the resolved SDK's bundled source in a temp dir and pip install it.

    Returns the installed version. Streams pip's own output instead of
    capturing it, matching the existing _cmd_deploy/_cmd_start convention.
    """
    with tempfile.TemporaryDirectory(prefix="tandem_sdk_install_") as tmp_dir:
        staged_path = Path(tmp_dir) / resolved.name
        shutil.copytree(resolved.bundle_path, staged_path)
        result = subprocess.run([target_python, "-m", "pip", "install", str(staged_path)])

    if result.returncode != 0:
        raise RuntimeError(f"pip install failed with exit code {result.returncode}. See the output above for details.")

    return resolved.version
