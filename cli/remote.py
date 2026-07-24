from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .app_config import load_project_config
from .auth import get_api_key, resolve_server_url
from .build import build_project

_REQUEST_TIMEOUT_SECONDS = 60
_POLL_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class DeployResult:
    name: str
    pid: str


@dataclass(frozen=True)
class StartResult:
    output_dir: Path
    pid: str
    job_id: str
    job_token: str
    status_url: str
    results_url: str
    status: str
    counts: dict[str, Any]


def _resolve_server_url(server_url: str | None) -> str:
    # One resolver for the whole CLI. Auth, deploy, start, and the node all point
    # at the same server, resolved the same way: an explicit --server-url wins,
    # then the saved setting, then TANDEM_SERVER_URL/SERVER_URL, then the default.
    # This used to have its own localhost default separate from auth's, which is
    # how the CLI could end up logging in to one server and deploying to another.
    # Keeping it a thin wrapper means node_service can still import it by name.
    return resolve_server_url(server_url)


def _resolve_api_key(api_key: str | None) -> str:
    resolved = (
        api_key or os.environ.get("TANDEM_API_KEY") or get_api_key() or ""
    ).strip()
    if not resolved:
        raise RuntimeError(
            "Missing API key. Run `tandem auth login` (or `tandem auth register`) "
            "to store one, or pass --api-key, or set TANDEM_API_KEY."
        )
    return resolved


def _headers(api_key: str, *, job_token: str | None = None) -> dict[str, str]:
    headers = {"X-API-Key": api_key}
    if job_token:
        headers["X-Job-Token"] = job_token
    return headers


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
            or json.dumps(payload, sort_keys=True)
        )
        if payload.get("details"):
            detail = f"{detail} - Details: {payload.get('details')}"
    else:
        detail = str(payload) or "request failed"

    raise RuntimeError(
        f"{response.request.method} {response.url} failed with {response.status_code}: {detail}"
    )


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Server response was missing `{field_name}`.")
    return value.strip()


def deploy_project(
    config_path: str | Path,
    *,
    server_url: str | None = None,
    api_key: str | None = None,
) -> DeployResult:
    config = load_project_config(config_path)
    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)

    with config.config_path.open("rb") as toml_handle:
        response = requests.post(
            f"{resolved_server_url}/deploy/",
            headers=_headers(resolved_api_key),
            files=[
                (
                    "toml_file",
                    (config.config_path.name, toml_handle, "application/toml"),
                )
            ],
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )

    if response.status_code != 201:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Deploy response was not valid JSON.")

    return DeployResult(
        name=_required_text(payload, "name"),
        pid=_required_text(payload, "pid"),
    )


def start_project(
    config_path: str | Path,
    *,
    server_url: str | None = None,
    api_key: str | None = None,
    pid: str | None = None,
    strict: bool = True,
) -> StartResult:
    config = load_project_config(config_path)
    build_result = build_project(config_path, strict=strict)
    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)
    resolved_pid = (pid or "").strip()

    if not resolved_pid:
        resolved_pid = deploy_project(
            config.config_path,
            server_url=resolved_server_url,
            api_key=resolved_api_key,
        ).pid

    handles = []
    try:
        toml_handle = config.config_path.open("rb")
        handles.append(toml_handle)
        manifest_handle = build_result.manifest_path.open("rb")
        handles.append(manifest_handle)

        # Keep this upload lean; the server only reads the TOML, manifest, and task blobs.
        files: list[tuple[str, tuple[str, Any, str]]] = [
            (
                "toml_file",
                (config.config_path.name, toml_handle, "application/toml"),
            ),
            (
                "manifest_file",
                (
                    build_result.manifest_path.name,
                    manifest_handle,
                    "application/json",
                ),
            ),
        ]

        # The node splits a task blob on the "TNDM" magic into the wasm module
        # and the JSON input handed to the component's `run` export. `tandem
        # start` runs each task once with no arguments, so we frame every wasm
        # with an empty [args, kwargs]. Without this the node would hand `run`
        # empty bytes and the task would trap on `json.loads(b"")`. Passing
        # arguments to a specific task is what the SDK's `.submit()` is for.
        empty_input = json.dumps([[], {}]).encode("utf-8")
        for wasm_path in build_result.wasm_paths:
            wasm_bytes = wasm_path.read_bytes()
            framed_blob = (
                b"TNDM"
                + len(wasm_bytes).to_bytes(4, "little")
                + wasm_bytes
                + empty_input
            )
            files.append(
                (
                    "wasm_files",
                    (wasm_path.name, framed_blob, "application/wasm"),
                )
            )

        response = requests.post(
            f"{resolved_server_url}/start/",
            headers=_headers(resolved_api_key),
            data={"pid": resolved_pid},
            files=files,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    finally:
        for handle in handles:
            handle.close()

    if response.status_code != 202:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Start response was not valid JSON.")

    counts = payload.get("counts")
    if not isinstance(counts, dict):
        counts = {}

    return StartResult(
        output_dir=build_result.output_dir,
        pid=resolved_pid,
        job_id=_required_text(payload, "job_id"),
        job_token=_required_text(payload, "job_token"),
        status_url=_required_text(payload, "status_url"),
        results_url=_required_text(payload, "results_url"),
        status=_required_text(payload, "status"),
        counts=counts,
    )


def fetch_job_results(
    start_result: StartResult,
    *,
    api_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    resolved_api_key = _resolve_api_key(api_key)
    response = requests.get(
        start_result.results_url,
        headers=_headers(resolved_api_key, job_token=start_result.job_token),
        timeout=_POLL_TIMEOUT_SECONDS,
    )

    if response.status_code not in {200, 202}:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Results response was not valid JSON.")

    return response.status_code, payload


def fetch_usage(
    *,
    server_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Fetch the account's resource usage from the server."""
    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)
    response = requests.get(
        f"{resolved_server_url}/api/v1/usage",
        headers=_headers(resolved_api_key),
        timeout=_POLL_TIMEOUT_SECONDS,
    )

    if response.status_code != 200:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Usage response was not valid JSON.")
    return payload


def fetch_node_specs(
    *, server_url: str, node_id: str, node_token: str
) -> dict[str, Any]:
    """Fetch this node's own record (hardware specs, last seen) from the server.

    Unlike the rest of this module, this authenticates as the node itself
    (X-Node-Id + its node_token) rather than with an account API key --
    that's the same identity the tandem-node binary uses.
    """
    response = requests.get(
        f"{server_url}/nodes/me",
        headers={"X-Node-Id": node_id, "Authorization": f"Bearer {node_token}"},
        timeout=_POLL_TIMEOUT_SECONDS,
    )

    if response.status_code != 200:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Node info response was not valid JSON.")
    return payload


def serve_deploy(
    *,
    project_root: str,
    start_command: str,
    replicas: int,
    name: str,
    server_url: str | None = None,
    api_key: str | None = None,
    sdk_path: Any = None,
    sdk_import_name: str = "tandem",
) -> dict[str, Any]:
    """Tar the project and hand it to the server to host on the nodes."""
    import io
    import pathlib
    import tarfile

    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)

    # Skip build output, caches, and VCS -- the app just needs its own source.
    skip = {".tandem_build", ".tandem", "__pycache__", ".git"}

    def _filter(tarinfo: tarfile.TarInfo):
        parts = tarinfo.name.split("/")
        if any(part in skip for part in parts):
            return None
        return tarinfo

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(str(pathlib.Path(project_root)), arcname=".", filter=_filter)
        # The serve sandbox launches the app with a bare `python3`, so bundle the
        # SDK package next to the app (mirroring what `tandem build` stages).
        # Without it the app's `import tandem` fails with ModuleNotFoundError on
        # the node.
        if sdk_path is not None:
            sdk_package = pathlib.Path(sdk_path) / sdk_import_name
            if sdk_package.is_dir():
                tar.add(str(sdk_package), arcname=sdk_import_name, filter=_filter)
    buffer.seek(0)

    response = requests.post(
        f"{resolved_server_url}/serve/deploy",
        headers=_headers(resolved_api_key),
        data={
            "start_command": start_command,
            "replicas": str(replicas),
            "name": name,
        },
        files={"bundle": ("bundle.tar", buffer, "application/x-tar")},
        timeout=60,
    )

    if response.status_code != 201:
        _raise_response_error(response)

    payload = _response_payload(response)
    if not isinstance(payload, dict):
        raise RuntimeError("Serve deploy response was not valid JSON.")
    return payload


def serve_list(*, server_url: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    """List the caller's serve deployments."""
    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)
    response = requests.get(
        f"{resolved_server_url}/serve",
        headers=_headers(resolved_api_key),
        timeout=30,
    )
    if response.status_code != 200:
        _raise_response_error(response)
    payload = _response_payload(response)
    return payload if isinstance(payload, dict) else {"deployments": []}


def serve_stop(
    *, pid: str, server_url: str | None = None, api_key: str | None = None
) -> dict[str, Any]:
    """Stop hosting a serve deployment and forget it."""
    resolved_server_url = _resolve_server_url(server_url)
    resolved_api_key = _resolve_api_key(api_key)
    response = requests.delete(
        f"{resolved_server_url}/serve/{pid}",
        headers=_headers(resolved_api_key),
        timeout=30,
    )
    if response.status_code != 200:
        _raise_response_error(response)
    payload = _response_payload(response)
    return payload if isinstance(payload, dict) else {"pid": pid}
