from __future__ import annotations

import argparse
import base64
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import os

import requests
from dotenv import find_dotenv, load_dotenv

from .analysis import Diagnostic
from .app_config import write_project_config
from .auth import (
    clear_auth_session,
    clear_stored_registration_token,
    clear_stored_server_url,
    get_stored_registration_token,
    get_stored_server_url,
    load_auth_session,
    login_user,
    mask_secret,
    prompt_password,
    prompt_username,
    register_user,
    set_stored_registration_token,
    set_stored_server_url,
    store_auth_session,
)
from .build import AnalysisFailure, build_project, clean_project, inspect_project
from .manifest import build_manifest
from .node_service import (
    active_backend,
    disable_service,
    enable_service,
    get_status,
    is_registered,
    node_is_running,
    register_node_now,
    resolve_node_server_url,
    resolve_registration_token,
    start_node,
    stop_node,
    tail_log,
)
from .remote import deploy_project, fetch_job_results, start_project
from .uninstall import perform_uninstall
from .sdk_commands import (
    download_sdk,
    fetch_sdk_registry,
    install_sdk,
    resolve_sdk,
    resolve_target_python,
)

# --- ANSI Color Constants ---
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def _format_diagnostic(diagnostic: Diagnostic) -> str:
    location = f"{diagnostic.file}:{diagnostic.line}:{diagnostic.column}"
    return (
        f"[{diagnostic.level}] {diagnostic.code} task={diagnostic.task} "
        f"{location} - {diagnostic.message}"
    )


def _add_remote_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--server-url",
        default=None,
        help="Server base URL. Falls back to the saved setting, then TANDEM_SERVER_URL, then https://tandem.wnusair.org.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="User API key. Falls back to TANDEM_API_KEY loaded from the environment or a local .env file.",
    )


def _add_auth_options(
    parser: argparse.ArgumentParser, *, include_rotate: bool = False
) -> None:
    parser.add_argument(
        "--server-url",
        default=None,
        help="Server base URL. Falls back to the saved setting, then TANDEM_SERVER_URL, then https://tandem.wnusair.org.",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="Username for the Tandem account. Prompts interactively when omitted.",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password for the Tandem account. Prefer omitting this flag so the CLI can prompt securely.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file where TANDEM_SERVER_URL and TANDEM_API_KEY should be stored. Defaults to .env in the current directory.",
    )
    parser.add_argument(
        "--no-store",
        action="store_true",
        help="Do not persist TANDEM_SERVER_URL or TANDEM_API_KEY to an env file.",
    )
    parser.add_argument(
        "--show-api-key",
        action="store_true",
        help="Print the full API key to stdout. By default the CLI only shows a masked value when it also stores the key in an env file.",
    )
    if include_rotate:
        parser.add_argument(
            "--rotate-api-key",
            action="store_true",
            help="Rotate any existing API key for this user before saving the authenticated session.",
        )


def _format_counts(counts: dict[str, Any]) -> str:
    ordered = ["queued", "claimed", "running", "completed", "failed"]
    parts: list[str] = []

    for key in ordered:
        if key in counts:
            parts.append(f"{key}={counts[key]}")

    for key in sorted(counts):
        if key not in ordered:
            parts.append(f"{key}={counts[key]}")

    return ", ".join(parts) if parts else "no counts yet"


def _decode_result_payload(result_b64: str) -> Any:
    raw = base64.b64decode(result_b64)
    if not raw:
        return None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"binary_b64": result_b64}

    stripped = text.strip()
    if not stripped:
        return None

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text


def _format_result_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    if value is None:
        return "null"
    return str(value)


def _task_label(item: dict[str, Any]) -> str:
    label = (
        str(item.get("task_name") or "").strip()
        or str(item.get("filename") or "").strip()
        or str(item.get("tid") or "task")
    )

    shard_total = item.get("shard_total")
    shard_index = item.get("shard_index")
    if (
        isinstance(shard_total, int)
        and isinstance(shard_index, int)
        and shard_total > 1
    ):
        label = f"{label} shard {shard_index + 1}/{shard_total}"

    assigned_node = str(item.get("assigned_node") or "").strip()
    if assigned_node:
        label = f"{label} on {assigned_node}"

    return label


def _print_job_results(payload: dict[str, Any]) -> None:
    job_id = payload.get("job_id") or ""
    status = payload.get("status") or "unknown"
    print(f"Job {job_id} finished with status {status}")

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        print("No task results were returned.")
        return

    for item in results:
        if not isinstance(item, dict):
            continue

        print(f"\n- {_task_label(item)}")
        if item.get("status") == "completed":
            result_b64 = item.get("result_b64")
            if isinstance(result_b64, str):
                print(_format_result_value(_decode_result_payload(result_b64)))
            else:
                print("completed, but no payload was returned")
            continue

        error_message = str(item.get("error") or "task failed")
        print(error_message)


def _report_analysis_failure(exc: AnalysisFailure) -> int:
    print(str(exc), file=sys.stderr)
    print("Diagnostics:", file=sys.stderr)
    for diagnostic in exc.report.diagnostics:
        print(f"  {_format_diagnostic(diagnostic)}", file=sys.stderr)
    return 1


_DEFAULT_INIT_CONFIG_PATH = "tandem.toml"
_DEFAULT_INIT_ENTRY = "tasks.py"
_DEFAULT_INIT_VERSION = "0.1.0"


def _default_init_project_name() -> str:
    current_dir = Path.cwd().name.strip()
    return current_dir or "tandem-project"


def _default_init_output_dir(project_name: str) -> str:
    return f".tandem_build/{project_name}"


def _prompt_init_value(*, label: str, description: str, default: str) -> str:
    print(f"\n{label}")
    print(f"  {description}")
    print(f"  Press Enter to accept the default: {default}")

    try:
        value = input("> ").strip()
    except EOFError as exc:  # pragma: no cover - depends on terminal state.
        raise ValueError(
            "Interactive init could not read input. Re-run in a terminal or pass values explicitly."
        ) from exc

    return value or default


def _resolve_init_values(
    args: argparse.Namespace,
) -> tuple[str, str, str, str | None, str]:
    version = args.version or _DEFAULT_INIT_VERSION

    if args.config_path and args.name and args.entry:
        return (
            args.config_path,
            args.name,
            args.entry,
            args.output_dir,
            version,
        )

    print("Create a new Tandem project config.")
    print("We'll ask a few quick questions. Defaults are shown for optional values.")

    config_path = args.config_path or _prompt_init_value(
        label="Config path",
        description="Where should Tandem write the project TOML file?",
        default=_DEFAULT_INIT_CONFIG_PATH,
    )

    project_name = args.name or _prompt_init_value(
        label="Project name",
        description="Used for the project metadata and the default build output folder.",
        default=_default_init_project_name(),
    )

    entry = args.entry or _prompt_init_value(
        label="Python entry file",
        description="Path to the Python file containing your Tandem-decorated tasks.",
        default=_DEFAULT_INIT_ENTRY,
    )

    resolved_version = args.version or _prompt_init_value(
        label="Project version",
        description="Semantic version written into the project config.",
        default=version,
    )

    output_dir = args.output_dir or _prompt_init_value(
        label="Build output directory",
        description="Directory for generated build artifacts.",
        default=_default_init_output_dir(project_name),
    )

    return config_path, project_name, entry, output_dir, resolved_version


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tandem",
        description="Discover Tandem SDK tasks, build `.wasm` artifacts, and run them through a Tandem server.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="Create a new Tandem project TOML file."
    )
    init_parser.add_argument(
        "config_path",
        nargs="?",
        default=None,
        help="Path to the TOML config to create. Prompts interactively when omitted.",
    )
    init_parser.add_argument(
        "--name",
        default=None,
        help="Project name. Defaults to the current directory name during interactive init.",
    )
    init_parser.add_argument(
        "--entry",
        default=None,
        help="Path to the Python source file containing Tandem-decorated tasks. Defaults to `tasks.py` during interactive init.",
    )
    init_parser.add_argument(
        "--output-dir",
        default=None,
        help="Build output directory relative to the config file. Defaults to `.tandem_build/<project-name>`.",
    )
    init_parser.add_argument(
        "--version",
        default=None,
        help="Project version. Defaults to `0.1.0`.",
    )

    inspect_parser = subparsers.add_parser(
        "inspect", help="Load a project, discover tasks, and print analysis output."
    )
    inspect_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )

    manifest_parser = subparsers.add_parser(
        "manifest",
        help="Print the generated manifest JSON to stdout without writing files.",
    )
    manifest_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )

    build_parser = subparsers.add_parser(
        "build",
        help="Validate the project and emit `.wasm` artifacts plus `.tandem/manifest.json`.",
    )
    build_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )
    build_parser.add_argument(
        "--allow-analysis-errors",
        action="store_true",
        help="Write artifacts even if static validation reports errors.",
    )

    auth_parser = subparsers.add_parser(
        "auth",
        help="Manage Tandem authentication (login, logout, register, status).",
    )
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    auth_register_parser = auth_subparsers.add_parser(
        "register",
        help="Create a new Tandem account and store credentials securely in the OS keyring.",
    )
    _add_auth_options(auth_register_parser)

    auth_login_parser = auth_subparsers.add_parser(
        "login",
        help="Authenticate with Tandem and store JWT tokens securely in the OS keyring.",
    )
    _add_auth_options(auth_login_parser, include_rotate=True)

    auth_logout_parser = auth_subparsers.add_parser(
        "logout",
        help="Revoke the current session on the server and clear all local credentials.",
    )
    auth_logout_parser.add_argument(
        "--server-url",
        default=None,
        help="Server base URL. Falls back to stored server URL or https://tandem.wnusair.org.",
    )

    auth_subparsers.add_parser(
        "status",
        help="Show the currently authenticated user and session info.",
    )

    settings_parser = subparsers.add_parser(
        "settings",
        help="View or change local CLI settings, like which server to talk to.",
    )
    settings_subparsers = settings_parser.add_subparsers(dest="settings_command", required=True)

    settings_subparsers.add_parser(
        "show",
        help="Show the server URL currently in effect, and where it came from.",
    )

    settings_set_server_url_parser = settings_subparsers.add_parser(
        "set-server-url",
        help="Save a server URL so you don't need --server-url on every command.",
    )
    settings_set_server_url_parser.add_argument(
        "server_url",
        help="Server base URL, e.g. https://tandem.example.com or http://127.0.0.1:6767",
    )

    settings_subparsers.add_parser(
        "reset-server-url",
        help="Remove the saved server URL, reverting to TANDEM_SERVER_URL/SERVER_URL or the default.",
    )

    settings_set_registration_token_parser = settings_subparsers.add_parser(
        "set-registration-token",
        help="For headless nodes only. Logged-in users don't need this.",
    )
    settings_set_registration_token_parser.add_argument(
        "registration_token",
        help="The bearer token your server's TANDEM_NODE_REGISTRATION_TOKEN is set to.",
    )

    settings_subparsers.add_parser(
        "reset-registration-token",
        help="Remove the saved registration token, reverting to TANDEM_NODE_REGISTRATION_TOKEN or none.",
    )

    sdk_parser = subparsers.add_parser(
        "sdk",
        help="Browse and fetch Tandem SDKs from the server's registry. Requires `tandem auth login`.",
    )
    sdk_subparsers = sdk_parser.add_subparsers(dest="sdk_command", required=True)

    sdk_subparsers.add_parser(
        "list",
        help="List SDKs (and their versions) available on the server.",
    )

    sdk_install_parser = sdk_subparsers.add_parser(
        "install",
        help="Pip install a Tandem SDK into the current Python environment.",
    )
    sdk_install_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="SDK name from `tandem sdk list`. Auto-selects when only one SDK is available.",
    )
    sdk_install_parser.add_argument(
        "--python",
        default=None,
        help="Python interpreter to install into. Defaults to the active virtualenv, "
        "falling back to the first `python3`/`python` found on PATH.",
    )

    sdk_download_parser = sdk_subparsers.add_parser(
        "download",
        help="Copy an SDK's source into a local folder without installing it.",
    )
    sdk_download_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="SDK name from `tandem sdk list`. Auto-selects when only one SDK is available.",
    )
    sdk_download_parser.add_argument(
        "--output",
        default=None,
        help="Directory to copy the SDK source into. Defaults to ./<name>.",
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Create a deployment on the server and print its pid.",
    )
    deploy_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )
    _add_remote_options(deploy_parser)

    start_parser = subparsers.add_parser(
        "start",
        help="Build the project, upload its `.wasm` artifacts, and optionally wait for results.",
    )
    start_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )
    start_parser.add_argument(
        "--pid",
        default=None,
        help="Existing deployment pid. If omitted, Tandem creates one first.",
    )
    start_parser.add_argument(
        "--allow-analysis-errors",
        action="store_true",
        help="Upload artifacts even if static validation reports errors.",
    )
    start_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Queue the job and return immediately instead of polling for results.",
    )
    start_parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between result polls while waiting.",
    )
    _add_remote_options(start_parser)

    clean_parser = subparsers.add_parser(
        "clean", help="Remove generated build artifacts."
    )
    clean_parser.add_argument(
        "config_path",
        nargs="?",
        default=os.environ.get("TANDEM_CONFIG_PATH", "tandem.toml"),
        help="Path to the Tandem TOML config. Defaults to tandem.toml.",
    )

    subparsers.add_parser(
        "status",
        help="Show your login and whether the Tandem node is running.",
    )

    usage_parser = subparsers.add_parser(
        "usage",
        help="Show how much of your account's resource limits you're using.",
    )
    _add_remote_options(usage_parser)

    serve_parser = subparsers.add_parser(
        "serve",
        help="Host a web app on your Tandem nodes, behind the load balancer.",
    )
    serve_parser.add_argument(
        "serve_args",
        nargs="*",
        help=(
            "A tandem.toml to deploy (the default), or `stop <pid>` to stop a "
            "running deployment, or `list` to show your deployments."
        ),
    )
    serve_parser.add_argument(
        "--start",
        help="Command that starts your app. Defaults to [build] start in tandem.toml.",
    )
    serve_parser.add_argument(
        "--replicas",
        type=int,
        default=2,
        help="How many nodes to run the app on. Default 2.",
    )
    _add_remote_options(serve_parser)

    node_parser = subparsers.add_parser(
        "node",
        help="Run and manage the background Tandem node on this machine.",
    )
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)

    node_start_parser = node_subparsers.add_parser(
        "start",
        help="Start the node in the background, registering it the first time.",
    )
    node_start_parser.add_argument(
        "--server-url",
        default=None,
        help="Server the node should connect to. Defaults to your saved server URL.",
    )

    node_subparsers.add_parser(
        "stop",
        help="Stop the background node.",
    )

    node_restart_parser = node_subparsers.add_parser(
        "restart",
        help="Stop and start the background node.",
    )
    node_restart_parser.add_argument(
        "--server-url",
        default=None,
        help="Server the node should connect to. Defaults to your saved server URL.",
    )

    node_subparsers.add_parser(
        "status",
        help="Show whether the node is running, its id, and how it's running.",
    )

    node_logs_parser = node_subparsers.add_parser(
        "logs",
        help="Print the most recent lines from the node's log.",
    )
    node_logs_parser.add_argument(
        "--lines",
        type=int,
        default=40,
        help="How many lines from the end of the log to show. Defaults to 40.",
    )

    node_enable_parser = node_subparsers.add_parser(
        "enable",
        help="Run the node 24/7 as an OS service (starts on boot, restarts on crash).",
    )
    node_enable_parser.add_argument(
        "--server-url",
        default=None,
        help="Server the node should connect to. Defaults to your saved server URL.",
    )

    node_subparsers.add_parser(
        "disable",
        help="Turn off the 24/7 OS service, going back to manual start/stop.",
    )

    subparsers.add_parser(
        "uninstall",
        help="Completely remove Tandem from this machine (asks you to confirm a code).",
    )

    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    config_path, name, entry, output_dir, version = _resolve_init_values(args)
    path = write_project_config(
        config_path,
        name=name,
        entry=entry,
        output_dir=output_dir,
        version=version,
    )
    print(f"Created Tandem project config at {path}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    config, discovered, report = inspect_project(args.config_path)
    sdk_info = discovered.sdk_descriptor.sdk
    print(f"Project: {config.name}")
    print(f"Runtime: {config.runtime}")
    print(f"Entry:   {config.entry_path}")
    print(f"Output:  {config.output_dir}")
    print(
        f"SDK:     {sdk_info.package} language={sdk_info.language} "
        f"version={sdk_info.version}"
    )
    print(f"Tasks:   {len(discovered.tasks)}")

    for descriptor in discovered.task_descriptors:
        metadata = descriptor.metadata
        params = ", ".join(metadata.parameters)
        print(f"  - {descriptor.export_name}: {metadata.kind}({params})")

    if report.diagnostics:
        print("\nDiagnostics:")
        for diagnostic in report.diagnostics:
            print(f"  {_format_diagnostic(diagnostic)}")
    else:
        print("\nDiagnostics: none")

    return 1 if report.has_errors else 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    config, discovered, _ = inspect_project(args.config_path)
    manifest = build_manifest(config, discovered)
    json.dump(manifest, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        result = build_project(
            args.config_path,
            strict=not args.allow_analysis_errors,
        )
    except AnalysisFailure as exc:
        return _report_analysis_failure(exc)

    print(f"{Colors.GREEN}{Colors.BOLD}Built {result.task_count} task(s) into {result.output_dir}{Colors.RESET}")
    print(f"{Colors.CYAN}Manifest:   {result.manifest_path}{Colors.RESET}")
    print(f"{Colors.CYAN}Analysis:   {result.analysis_path}{Colors.RESET}")
    print(f"{Colors.CYAN}SDK bridge: {result.sdk_bridge_path}{Colors.RESET}")
    print(f"{Colors.BOLD}WASM artifacts:{Colors.RESET}")
    for path in result.wasm_paths:
        print(f"  - {Colors.BLUE}{path}{Colors.RESET}")

    if result.diagnostics:
        print(f"\n{Colors.YELLOW}Diagnostics:{Colors.RESET}")
        for diagnostic in result.diagnostics:
            print(f"  {_format_diagnostic(diagnostic)}")

    return 0


def _warn_on_password_flag(args: argparse.Namespace) -> None:
    if getattr(args, "password", None) is not None:
        print(
            "warning: passing passwords via --password can leak them into shell history and process lists; prefer the secure interactive prompt when possible.",
            file=sys.stderr,
        )


def _print_auth_result(
    *,
    action: str,
    username: str,
    api_key: str,
    env_path: Path | None,
    show_api_key: bool,
) -> None:
    print(f"{action} user: {username}")
    print("Credentials stored securely in OS keyring.")
    print(f"API key: {api_key if show_api_key else mask_secret(api_key)}")


def _cmd_auth_register(args: argparse.Namespace) -> int:
    _warn_on_password_flag(args)
    username = prompt_username(args.username)
    password = prompt_password(args.password, confirm=args.password is None)

    register_user(
        username=username,
        password=password,
        server_url=args.server_url,
    )
    session = login_user(
        username=username,
        password=password,
        server_url=args.server_url,
    )

    store_auth_session(session)
    _print_auth_result(
        action="Registered",
        username=session.username,
        api_key=session.api_key,
        env_path=None,
        show_api_key=getattr(args, 'show_api_key', False),
    )
    return 0


def _cmd_auth_login(args: argparse.Namespace) -> int:
    _warn_on_password_flag(args)
    username = prompt_username(args.username)
    password = prompt_password(args.password)

    session = login_user(
        username=username,
        password=password,
        server_url=args.server_url,
        rotate_api_key=getattr(args, 'rotate_api_key', False),
    )

    store_auth_session(session)
    _print_auth_result(
        action="Authenticated",
        username=session.username,
        api_key=session.api_key,
        env_path=None,
        show_api_key=getattr(args, 'show_api_key', False),
    )
    return 0


def _cmd_auth_logout(args: argparse.Namespace) -> int:
    server_url = getattr(args, 'server_url', None)
    clear_auth_session(server_url=server_url)
    print("Logged out. Session revoked on server and all local credentials cleared.")
    return 0


def _cmd_auth_status(_args: argparse.Namespace) -> int:
    session = load_auth_session()
    if session is None:
        print("Not logged in. Run `tandem auth login` to authenticate.")
        return 1
    print(f"Logged in as: {session.username}")
    print(f"Server:       {session.server_url}")
    print(f"API key:      {mask_secret(session.api_key)}")
    return 0


def _describe_server_url() -> None:
    """Print the server URL that's currently in effect, and why."""
    stored = get_stored_server_url()
    if stored:
        print(f"Server URL: {stored}")
        print("Source:     saved setting (tandem settings set-server-url)")
        print("This is used by every command unless you pass --server-url.")
        return

    env_url = os.environ.get("TANDEM_SERVER_URL") or os.environ.get("SERVER_URL")
    if env_url:
        env_name = "TANDEM_SERVER_URL" if os.environ.get("TANDEM_SERVER_URL") else "SERVER_URL"
        print(f"Server URL: {env_url}")
        print(f"Source:     {env_name} environment variable")
        print("This is used by every command unless you pass --server-url.")
        return

    print("No server URL is saved, so every command falls back to the same default:")
    print("  https://tandem.wnusair.org")
    print(
        "\nFor a local server, run `tandem settings set-server-url http://127.0.0.1:6767` "
        "(or set TANDEM_SERVER_URL) to point everything there."
    )


def _describe_registration_token() -> None:
    """Print the node registration token in effect and where it came from. Only
    headless nodes need one; logged-in users register under their account."""
    stored = get_stored_registration_token()
    if stored:
        print(f"\nNode registration token: {mask_secret(stored)}")
        print("Source:                  saved setting (tandem settings set-registration-token)")
        return

    env_token = os.environ.get("TANDEM_NODE_REGISTRATION_TOKEN")
    if env_token:
        print(f"\nNode registration token: {mask_secret(env_token)}")
        print("Source:                  TANDEM_NODE_REGISTRATION_TOKEN environment variable")
        return

    print("\nNo node registration token saved. If you're logged in you don't need one.")
    print("Headless nodes: tandem settings set-registration-token <token>")


def _cmd_settings_show(_args: argparse.Namespace) -> int:
    _describe_server_url()
    _describe_registration_token()
    return 0


def _cmd_settings_set_server_url(args: argparse.Namespace) -> int:
    normalized = set_stored_server_url(args.server_url)
    print(f"{Colors.GREEN}Saved server URL: {normalized}{Colors.RESET}")
    print("Every command will use this now, unless you pass --server-url.")
    return 0


def _cmd_settings_reset_server_url(_args: argparse.Namespace) -> int:
    clear_stored_server_url()
    print(f"{Colors.GREEN}Cleared the saved server URL.{Colors.RESET}")
    print("Commands will fall back to TANDEM_SERVER_URL/SERVER_URL or their built-in default.")
    return 0


def _cmd_settings_set_registration_token(args: argparse.Namespace) -> int:
    saved = set_stored_registration_token(args.registration_token)
    print(f"{Colors.GREEN}Saved node registration token: {mask_secret(saved)}{Colors.RESET}")
    print("`tandem node start` will send this automatically now -- no more exporting it by hand.")
    return 0


def _cmd_settings_reset_registration_token(_args: argparse.Namespace) -> int:
    clear_stored_registration_token()
    print(f"{Colors.GREEN}Cleared the saved registration token.{Colors.RESET}")
    print("Falls back to TANDEM_NODE_REGISTRATION_TOKEN if that's still exported.")
    return 0


def _format_sdk_versions(sdk: dict[str, Any]) -> str:
    versions = [v for v in (sdk.get("versions") or []) if isinstance(v, dict)]
    if versions:
        return ", ".join(v.get("version", "?") for v in versions)
    if sdk.get("version"):
        return str(sdk["version"])
    return "(no versions listed)"


def _cmd_sdk_list(_args: argparse.Namespace) -> int:
    session, sdks = fetch_sdk_registry()
    print(f"Tandem SDKs on {session.server_url} (logged in as {session.username}):\n")

    if not sdks:
        print("No SDKs are registered on the server yet.")
        return 0

    for sdk in sdks:
        print(f"{sdk.get('name')}  ({sdk.get('language') or 'unknown language'})")
        if sdk.get("description"):
            print(f"  {sdk['description']}")
        print(f"  Versions: {_format_sdk_versions(sdk)}\n")

    print("Run `tandem sdk install` to install one.")
    return 0


def _cmd_sdk_install(args: argparse.Namespace) -> int:
    resolved = resolve_sdk(args.name)
    if resolved.warning:
        print(f"{Colors.YELLOW}warning: {resolved.warning}{Colors.RESET}", file=sys.stderr)

    python_bin = resolve_target_python(args.python)
    print(f"Installing into: {python_bin}")

    version = install_sdk(resolved, target_python=python_bin)
    print(f"{Colors.GREEN}{Colors.BOLD}Installed {resolved.name} {version}.{Colors.RESET}")
    print('Try: python -c "import tandem; print(tandem.__version__)"')
    return 0


def _cmd_sdk_download(args: argparse.Namespace) -> int:
    resolved = resolve_sdk(args.name)
    if resolved.warning:
        print(f"{Colors.YELLOW}warning: {resolved.warning}{Colors.RESET}", file=sys.stderr)

    output_dir = (
        Path(args.output).expanduser().resolve() if args.output else Path.cwd() / resolved.name
    )
    destination = download_sdk(resolved, output_dir)

    print(f"{Colors.GREEN}Downloaded {resolved.name} {resolved.version} into {destination}{Colors.RESET}")
    print(f"To install it yourself later: pip install {destination}")
    return 0


def _require_node_running() -> None:
    """The lock: deploy/start only make sense if this machine's node is up to run
    the work. Set TANDEM_SKIP_NODE_CHECK=1 to bypass (handy in CI)."""
    if os.environ.get("TANDEM_SKIP_NODE_CHECK"):
        return
    if not node_is_running():
        raise RuntimeError(
            "The Tandem node isn't running, so there's no worker to run your job.\n"
            "  Start it now:   tandem node start\n"
            "  Or run it in the background: tandem node enable\n"
            "  Check status:   tandem status\n"
            "To bypass this check (e.g. in CI), set TANDEM_SKIP_NODE_CHECK=1."
        )


def _backend_label(backend: str) -> str:
    return {
        "systemd": "systemd service",
        "launchd": "launchd service",
        "daemon": "background process",
    }.get(backend, backend)


def _format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return ""
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def _print_node_status() -> None:
    status = get_status()
    if status.running:
        print(f"{Colors.GREEN}Node: running{Colors.RESET}")
    else:
        print(f"{Colors.YELLOW}Node: stopped{Colors.RESET}")

    if status.node_id:
        print(f"  Node ID:  {status.node_id}")
    else:
        print("  Not registered yet -- it registers the first time you start it.")

    if status.server_url:
        print(f"  Server:   {status.server_url}")

    print(f"  Mode:     {_backend_label(active_backend())}")

    if status.pid:
        print(f"  PID:      {status.pid}")
    if status.uptime_seconds is not None:
        print(f"  Uptime:   {_format_uptime(status.uptime_seconds)}")

    if not status.running:
        print("  Start it with:  tandem node start")

    if not status.node_id and load_auth_session() is None and not resolve_registration_token():
        print("  Not logged in. Run `tandem auth login`, then `tandem node start`.")


def _cmd_status(_args: argparse.Namespace) -> int:
    session = load_auth_session()
    if session is None:
        print(f"{Colors.YELLOW}Auth: not logged in{Colors.RESET} (run `tandem auth login`)")
    else:
        print(f"{Colors.GREEN}Auth: logged in as {session.username}{Colors.RESET}")
        print(f"  Server:   {session.server_url}")

    print("")
    _print_node_status()
    return 0


def _fmt_amount(value, unit: str) -> str:
    """Format a usage amount for humans: GiB for bytes, commas for fuel."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value} {unit}"
    if unit == "bytes":
        return f"{number / (2 ** 30):.2f} GiB"
    if unit == "fuel":
        return f"{int(number):,} fuel"
    return f"{number:g} {unit}"


def _print_usage_bar(metric: dict) -> None:
    resource_type = str(metric.get("type", "?"))
    percent = float(metric.get("percent", 0.0) or 0.0)
    unit = str(metric.get("unit", ""))
    source = str(metric.get("source", ""))

    filled = max(0, min(20, int(round(percent / 100.0 * 20))))
    bar = "#" * filled + "-" * (20 - filled)

    used = _fmt_amount(metric.get("used", 0), unit)
    limit = _fmt_amount(metric.get("limit", 0), unit)
    tag = f"  {Colors.YELLOW}(placeholder){Colors.RESET}" if source == "placeholder" else ""

    print(f"  {resource_type:<12} [{bar}] {percent:5.1f}%   {used} / {limit}{tag}")


def _cmd_usage(args: argparse.Namespace) -> int:
    from .remote import fetch_usage

    payload = fetch_usage(
        server_url=getattr(args, "server_url", None),
        api_key=getattr(args, "api_key", None),
    )

    resources = payload.get("resources", [])
    if not resources:
        print("No usage information was returned.")
        return 0

    print("Account resource usage:")
    print("")
    for metric in resources:
        _print_usage_bar(metric)
    print("")
    print(
        f"{Colors.YELLOW}(placeholder){Colors.RESET} metrics aren't measured yet "
        "-- the limit is shown but usage is a stub."
    )
    return 0


def _cmd_serve_list(args: argparse.Namespace) -> int:
    from .remote import serve_list

    data = serve_list(
        server_url=getattr(args, "server_url", None),
        api_key=getattr(args, "api_key", None),
    )
    deployments = data.get("deployments") or []
    if not deployments:
        print("No serve deployments.")
        return 0
    for dep in deployments:
        nodes = dep.get("serving_nodes") or []
        where = ", ".join(nodes) if nodes else "no node serving"
        status = dep.get("status", "running")
        print(f"{dep.get('pid')}  {dep.get('name', '')}  [{status}]  ({where})")
        print(f"    {dep.get('url', '')}")
    return 0


def _cmd_serve_stop(args: argparse.Namespace, pid: str) -> int:
    from .remote import serve_stop

    serve_stop(
        pid=pid,
        server_url=getattr(args, "server_url", None),
        api_key=getattr(args, "api_key", None),
    )
    print(f"Stopped serve deployment {pid}.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .app_config import load_project_config
    from .remote import serve_deploy

    # `serve` doubles as a small management command:
    #   tandem serve [config.toml]   -> deploy (default)
    #   tandem serve stop <pid>      -> stop a running deployment
    #   tandem serve list           -> show your deployments
    serve_args = list(getattr(args, "serve_args", None) or [])
    verb = serve_args[0] if serve_args else None

    if verb == "list":
        return _cmd_serve_list(args)
    if verb == "stop":
        if len(serve_args) < 2:
            print("Usage: tandem serve stop <pid>", file=sys.stderr)
            return 1
        return _cmd_serve_stop(args, serve_args[1])

    config_path = serve_args[0] if serve_args else os.environ.get(
        "TANDEM_CONFIG_PATH", "tandem.toml"
    )
    config = load_project_config(config_path)
    start_command = getattr(args, "start", None) or config.build_start
    if not start_command:
        print(
            "No start command found. Set `start` under [build] in tandem.toml, "
            'or pass --start "...".'
        )
        return 1

    # If the project has an install step, run it now. Your nodes run the app
    # with no network, so anything it needs from pip (Flask, requests, ...) has
    # to be installed into the project folder here first and shipped inside the
    # bundle. A typical [build] install is something like:
    #   pip install flask --target libs
    if config.build_install:
        print(f"Installing dependencies: {config.build_install}")
        install = subprocess.run(config.build_install, shell=True, cwd=str(config.project_root))
        if install.returncode != 0:
            print("Dependency install failed, so the app was not deployed.", file=sys.stderr)
            return 1

    result = serve_deploy(
        project_root=str(config.project_root),
        start_command=start_command,
        replicas=args.replicas,
        name=config.name,
        server_url=getattr(args, "server_url", None),
        api_key=getattr(args, "api_key", None),
        sdk_path=config.sdk_path,
        sdk_import_name=config.sdk_import_name,
    )

    pid = result.get("pid", "?")
    url = result.get("url", "?")
    print(f"{Colors.GREEN}Hosting '{config.name}' as {pid}{Colors.RESET}")
    print(f"  Reachable at {url} through the Tandem load balancer.")
    print(f"  Running on up to {args.replicas} node(s).")
    return 0


def _registration_failure_hint(message: str) -> str | None:
    """A next step for registration failures we recognize, or None otherwise."""
    if "registration bearer token" in message or "registration token" in message:
        return (
            "Log in and this machine registers under your account:\n"
            "  tandem auth login\n"
            "For a headless node, save the server's shared token instead:\n"
            "  tandem settings set-registration-token <token>"
        )
    return None


def _register_if_needed(server_url: str) -> int | None:
    """Register this machine if it hasn't been yet, telling the user how it went.
    Returns an exit code to bail out with on failure, or None to keep going."""
    if is_registered():
        return None

    # Registering needs either a login or a saved token. Catch the common case
    # early instead of letting the node fail with a 401.
    if load_auth_session() is None and not resolve_registration_token():
        print(f"{Colors.YELLOW}Log in first:{Colors.RESET}  tandem auth login", file=sys.stderr)
        return 1

    print(f"Registering this machine as a Tandem node on {server_url}...")
    result = register_node_now(server_url)
    if not result.ok:
        print(f"{Colors.RED}Registration failed: {result.message}{Colors.RESET}", file=sys.stderr)
        hint = _registration_failure_hint(result.message)
        if hint:
            print(hint, file=sys.stderr)
        return 1
    print(f"{Colors.GREEN}Registered as {result.node_id}.{Colors.RESET}")
    return None


def _cmd_node_start(args: argparse.Namespace) -> int:
    server_url = resolve_node_server_url(getattr(args, "server_url", None))
    if node_is_running():
        print("The Tandem node is already running.")
        return 0

    bail = _register_if_needed(server_url)
    if bail is not None:
        return bail

    start_node(server_url)
    backend = active_backend()
    if backend in ("systemd", "launchd"):
        print(f"{Colors.GREEN}Tandem node started as a {_backend_label(backend)}.{Colors.RESET}")
    else:
        print(f"{Colors.GREEN}Tandem node started in the background.{Colors.RESET}")
        print("It keeps running after you close this terminal.")
    print("Check on it any time with:  tandem status")
    return 0


def _cmd_node_stop(_args: argparse.Namespace) -> int:
    stopped = stop_node()
    if stopped:
        print("Tandem node stopped.")
    else:
        print("The Tandem node wasn't running.")

    if active_backend() in ("systemd", "launchd"):
        print("Note: the OS service is still enabled, so it starts again on reboot.")
        print("Run `tandem node disable` to turn that off.")
    return 0


def _cmd_node_restart(args: argparse.Namespace) -> int:
    server_url = resolve_node_server_url(getattr(args, "server_url", None))
    stop_node()

    bail = _register_if_needed(server_url)
    if bail is not None:
        return bail

    start_node(server_url)
    print(f"{Colors.GREEN}Tandem node restarted.{Colors.RESET}")
    return 0


def _cmd_node_status(_args: argparse.Namespace) -> int:
    _print_node_status()
    return 0


def _cmd_node_logs(args: argparse.Namespace) -> int:
    text = tail_log(getattr(args, "lines", 40))
    if not text:
        print("No node log yet. Start the node with `tandem node start`.")
        return 0
    print(text)
    return 0


def _cmd_node_enable(args: argparse.Namespace) -> int:
    server_url = resolve_node_server_url(getattr(args, "server_url", None))

    bail = _register_if_needed(server_url)
    if bail is not None:
        return bail

    notes = enable_service(server_url)
    print(
        f"{Colors.GREEN}Tandem node is now running 24/7 as a "
        f"{_backend_label(active_backend())}.{Colors.RESET}"
    )
    for note in notes:
        print(f"  {note}")
    return 0


def _cmd_node_disable(_args: argparse.Namespace) -> int:
    kind = disable_service()
    if kind == "none":
        print("No OS service was enabled.")
    else:
        print("Turned off the 24/7 service. The node is back to manual start/stop.")
        print("Run `tandem node start` to run it in the background for this session.")
    return 0


def _generate_confirmation_code() -> str:
    """A fresh random 6-digit code the user has to type back to confirm uninstall.
    It's shown on screen each run, so it can't be guessed or piped in ahead of time."""
    return f"{secrets.randbelow(900000) + 100000}"


def _cmd_uninstall(_args: argparse.Namespace) -> int:
    print(f"{Colors.BOLD}This permanently removes Tandem from this machine.{Colors.RESET}")
    print("  - stops the node and any 24/7 service")
    print("  - clears your saved login")
    print("  - deletes ~/.tandem (the private environment, node binary, and node state)")
    print("  - removes the `tandem` command")
    print("")

    code = _generate_confirmation_code()
    print(f"To confirm, type this 6-digit code back:  {Colors.BOLD}{code}{Colors.RESET}")
    try:
        entered = input("Code: ").strip()
    except EOFError:
        print("No input received; nothing was removed.", file=sys.stderr)
        return 1

    if entered != code:
        print(f"{Colors.YELLOW}That code didn't match. Nothing was removed.{Colors.RESET}")
        return 1

    print("")
    for step in perform_uninstall():
        print(f"  - {step}")

    print("")
    print(f"{Colors.GREEN}Tandem has been removed.{Colors.RESET}")
    print("Re-run the installer any time to set it back up.")
    return 0


def _cmd_deploy(args: argparse.Namespace) -> int:
    # Compute deployments always go through the server -- it's the distributor
    # that hands work out to nodes. Web apps are a separate path: `tandem serve`.
    _require_node_running()
    result = deploy_project(
        args.config_path,
        server_url=args.server_url,
        api_key=args.api_key,
    )
    print(f"Deployment name: {result.name}")
    print(f"PID: {result.pid}")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    # Build the tasks to wasm, upload them, and let the server fan them out to
    # nodes -- then wait for results. Hosting a web app is `tandem serve`, not
    # this. A project can do both: its @compute tasks run here, its app serves.
    _require_node_running()
    try:
        result = start_project(
            args.config_path,
            server_url=args.server_url,
            api_key=args.api_key,
            pid=args.pid,
            strict=not args.allow_analysis_errors,
        )
    except AnalysisFailure as exc:
        return _report_analysis_failure(exc)

    print(f"{Colors.GREEN}{Colors.BOLD}Built artifacts into {result.output_dir}{Colors.RESET}")
    print(f"{Colors.CYAN}Deployment pid: {result.pid}{Colors.RESET}")
    print(f"{Colors.MAGENTA}Queued job: {result.job_id}{Colors.RESET}")
    print(f"{Colors.YELLOW}Initial counts: {_format_counts(result.counts)}{Colors.RESET}")

    if args.no_wait:
        print(f"{Colors.BLUE}Job token:   {result.job_token}{Colors.RESET}")
        print(f"{Colors.BLUE}Status URL:  {result.status_url}{Colors.RESET}")
        print(f"{Colors.BLUE}Results URL: {result.results_url}{Colors.RESET}")
        return 0

    print(f"\n{Colors.BOLD}Waiting for results...{Colors.RESET}")
    last_counts: dict[str, Any] | None = result.counts
    poll_interval = max(args.poll_interval, 0.1)

    while True:
        status_code, payload = fetch_job_results(result, api_key=args.api_key)
        raw_counts = payload.get("counts")
        counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}

        if counts != last_counts:
            print(f"Counts: {_format_counts(counts)}")
            last_counts = counts

        if status_code == 200:
            _print_job_results(payload)
            return 0 if payload.get("status") == "completed" else 1

        time.sleep(poll_interval)


def _cmd_clean(args: argparse.Namespace) -> int:
    output_dir = clean_project(args.config_path)
    print(f"Removed build directory {output_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(find_dotenv(usecwd=True))

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "inspect":
            return _cmd_inspect(args)
        if args.command == "manifest":
            return _cmd_manifest(args)
        if args.command == "build":
            return _cmd_build(args)
        if args.command == "auth":
            if args.auth_command == "register":
                return _cmd_auth_register(args)
            if args.auth_command == "login":
                return _cmd_auth_login(args)
            if args.auth_command == "logout":
                return _cmd_auth_logout(args)
            if args.auth_command == "status":
                return _cmd_auth_status(args)
            parser.error(f"Unknown auth command: {args.auth_command}")
        if args.command == "settings":
            if args.settings_command == "show":
                return _cmd_settings_show(args)
            if args.settings_command == "set-server-url":
                return _cmd_settings_set_server_url(args)
            if args.settings_command == "reset-server-url":
                return _cmd_settings_reset_server_url(args)
            if args.settings_command == "set-registration-token":
                return _cmd_settings_set_registration_token(args)
            if args.settings_command == "reset-registration-token":
                return _cmd_settings_reset_registration_token(args)
            parser.error(f"Unknown settings command: {args.settings_command}")
        if args.command == "sdk":
            if args.sdk_command == "list":
                return _cmd_sdk_list(args)
            if args.sdk_command == "install":
                return _cmd_sdk_install(args)
            if args.sdk_command == "download":
                return _cmd_sdk_download(args)
            parser.error(f"Unknown sdk command: {args.sdk_command}")
        if args.command == "deploy":
            return _cmd_deploy(args)
        if args.command == "start":
            return _cmd_start(args)
        if args.command == "clean":
            return _cmd_clean(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "usage":
            return _cmd_usage(args)
        if args.command == "serve":
            return _cmd_serve(args)
        if args.command == "node":
            if args.node_command == "start":
                return _cmd_node_start(args)
            if args.node_command == "stop":
                return _cmd_node_stop(args)
            if args.node_command == "restart":
                return _cmd_node_restart(args)
            if args.node_command == "status":
                return _cmd_node_status(args)
            if args.node_command == "logs":
                return _cmd_node_logs(args)
            if args.node_command == "enable":
                return _cmd_node_enable(args)
            if args.node_command == "disable":
                return _cmd_node_disable(args)
            parser.error(f"Unknown node command: {args.node_command}")
        if args.command == "uninstall":
            return _cmd_uninstall(args)
    except requests.exceptions.ConnectionError as exc:
        url = getattr(getattr(exc, "request", None), "url", None)
        where = f" at {url}" if url else ""
        print(
            f"error: Couldn't reach the Tandem server{where}. "
            "Check that it's running and that your server URL is correct "
            "(`tandem settings show`).",
            file=sys.stderr,
        )
        return 1
    except requests.exceptions.Timeout as exc:
        url = getattr(getattr(exc, "request", None), "url", None)
        where = f" at {url}" if url else ""
        print(
            f"error: Timed out waiting for the Tandem server to respond{where}. "
            "It may be slow, overloaded, or unreachable.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
