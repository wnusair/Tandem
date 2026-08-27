"""Managing the Tandem node as a background service from the CLI.

The node is a separate Rust program, but from a user's point of view it should
feel like part of `tandem`: you start it, stop it, and check on it with the CLI,
and the CLI points it at the same server you're already logged into. This module
is the glue that makes that happen.

There are two ways the node can run in the background:

  * as a plain detached process the CLI launches and tracks with a pid file
    (the default -- works everywhere, survives closing your terminal), or
  * as a real OS service (systemd on Linux, launchd on macOS) that also comes
    back on reboot and restarts itself if it crashes. You opt into this with
    `tandem node enable`.

Everything below is organized bottom-up: first identity/registration, then the
low-level process helpers, then the two backends, and finally the small dispatch
layer that the CLI commands actually call.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .auth import get_api_key, get_stored_registration_token
from .node_paths import (
    NODE_HOME,
    ensure_home,
    find_node_binary,
    installed_binary,
    log_file,
    pid_file,
    private_key_file,
    state_file,
)
from .remote import _resolve_server_url as resolve_job_server_url


@dataclass
class RegistrationResult:
    ok: bool
    node_id: str | None
    message: str


@dataclass
class NodeStatus:
    running: bool
    backend: str  # "daemon", "systemd", "launchd", or "none"
    pid: int | None
    node_id: str | None
    server_url: str | None
    registered_at: int | None
    uptime_seconds: int | None


def resolve_node_server_url(server_url: str | None = None) -> str:
    """Point the node at the same server deploy/start use.

    This matters: the lock ("can't deploy unless the node is running") only makes
    sense if the node is polling the exact server your jobs land on. So we reuse
    the deploy/start resolver rather than the auth one, which has a different
    default.
    """
    return resolve_job_server_url(server_url)


def resolve_registration_token() -> str:
    """The bearer token to send when registering with a server that requires one.

    Checked in the same order as the saved server URL: a setting saved with
    `tandem settings set-registration-token` wins first (so it survives across
    terminal sessions), then TANDEM_NODE_REGISTRATION_TOKEN in the environment
    for people who'd rather export it than save it. Empty string means "no
    token" -- most servers don't require one."""
    return get_stored_registration_token() or os.environ.get("TANDEM_NODE_REGISTRATION_TOKEN") or ""


def build_node_env(server_url: str) -> dict[str, str]:
    """The environment a launched node inherits: the current environment plus the
    server URL and the paths to its home files. We deliberately drop any node
    identity coming from the environment so the saved state file is the single
    source of truth for who this machine is."""
    env = dict(os.environ)
    env["TANDEM_SERVER_URL"] = server_url
    env["TANDEM_NODE_STATE_PATH"] = str(state_file())
    env["TANDEM_PRIVATE_KEY_PATH"] = str(private_key_file())
    env.pop("TANDEM_NODE_ID", None)
    env.pop("TANDEM_NODE_TOKEN", None)
    env.pop("TANDEM_NODE_REGISTER_ONLY", None)
    registration_token = resolve_registration_token()
    if registration_token:
        env["TANDEM_NODE_REGISTRATION_TOKEN"] = registration_token
    return env


def _missing_binary_message() -> str:
    return (
        "Could not find the tandem-node binary.\n"
        f"Expected it at {installed_binary()} (installed by ./install.sh),\n"
        "or set TANDEM_NODE_BIN to point at a prebuilt binary."
    )


def load_identity() -> dict | None:
    """Read the saved node identity, or None if the node has never registered."""
    path = state_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_registered() -> bool:
    identity = load_identity()
    return bool(identity and identity.get("node_id"))


def _clean_node_error(stderr: str) -> str:
    """Pull the most useful line out of the node's stderr for a failure message.
    The node prints a `[node] FATAL: ...` line when registration fails; surface
    that if we can, otherwise fall back to the last non-empty line."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in lines:
        if "FATAL" in line:
            return line.split("FATAL:", 1)[-1].strip() or line
    return lines[-1] if lines else "registration failed"


def register_node_now(server_url: str, *, timeout: float = 120.0) -> RegistrationResult:
    """Register this machine as a node, synchronously, and report the result.

    Runs the binary in its register-only mode, which generates the keypair (if
    needed), talks to the server, saves the identity, and exits. We run it up
    front -- rather than letting the background process register silently -- so
    the user actually sees "registered as node_xyz" or a clear error."""
    binary = find_node_binary()
    if binary is None:
        return RegistrationResult(False, None, _missing_binary_message())

    ensure_home()
    env = build_node_env(server_url)
    env["TANDEM_NODE_REGISTER_ONLY"] = "1"

    # If you're logged in, register under your account with the saved API key.
    # Only needed for this one-shot registration, so it's not in build_node_env.
    api_key = get_api_key()
    if api_key:
        env["TANDEM_NODE_AUTH_TOKEN"] = api_key

    try:
        completed = subprocess.run(
            [str(binary)],
            env=env,
            cwd=str(NODE_HOME),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return RegistrationResult(False, None, "Registration timed out talking to the server.")

    if completed.returncode != 0:
        return RegistrationResult(False, None, _clean_node_error(completed.stderr))

    identity = load_identity()
    node_id = (identity or {}).get("node_id")
    return RegistrationResult(True, node_id, "")


def _read_pid() -> int | None:
    path = pid_file()
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(pid: int) -> None:
    ensure_home()
    pid_file().write_text(str(pid), encoding="utf-8")


def _clear_pid() -> None:
    try:
        pid_file().unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int | None) -> bool:
    """Is a process with this pid currently alive? Written to work on both POSIX
    and Windows, since the node is meant to run on all three platforms."""
    if not pid or pid <= 0:
        return False

    if os.name == "nt":
        import ctypes

        process_query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        still_active = 259  # STILL_ACTIVE
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, we just don't own it. Good enough for a liveness check.
        return True
    return True


def _pid_is_node(pid: int) -> bool:
    """Guard against pid reuse: on Linux we can confirm the process really is our
    node binary by reading /proc. Anywhere else we can't cheaply check, so we
    trust the pid file."""
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    if not cmdline_path.exists():
        return True
    try:
        parts = cmdline_path.read_bytes().split(b"\x00")
    except OSError:
        return True
    return any(b"tandem-node" in part for part in parts)


def _terminate(pid: int, *, force: bool = False) -> None:
    if os.name == "nt":
        import ctypes

        process_terminate = 0x0001  # PROCESS_TERMINATE
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_terminate, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
        return

    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _daemon_running() -> bool:
    pid = _read_pid()
    if pid is None:
        return False
    if not _pid_alive(pid):
        _clear_pid()  # stale pid file left over from a crash or reboot
        return False
    return _pid_is_node(pid)


def _spawn_detached(binary: Path, env: dict[str, str]):
    """Launch the node fully detached from this terminal, logging to node.log."""
    ensure_home()
    log_handle = open(log_file(), "a", buffering=1, encoding="utf-8")

    popen_kwargs: dict = {
        "env": env,
        "cwd": str(NODE_HOME),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }

    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP -- no console, survives the
        # parent shell closing.
        popen_kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        # start_new_session detaches us from the controlling terminal so the node
        # keeps running after you close the shell.
        popen_kwargs["start_new_session"] = True

    try:
        return subprocess.Popen([str(binary)], **popen_kwargs)
    finally:
        # The child holds its own copy of the fd; ours isn't needed anymore.
        log_handle.close()


def _start_daemon(server_url: str) -> None:
    if _daemon_running():
        return

    binary = find_node_binary()
    if binary is None:
        raise RuntimeError(_missing_binary_message())

    env = build_node_env(server_url)
    proc = _spawn_detached(binary, env)
    _write_pid(proc.pid)

    # Give it a beat, then make sure it didn't immediately fall over (bad server
    # URL, missing key, etc.) so we don't report success on a dead process.
    time.sleep(0.6)
    if not _pid_alive(proc.pid):
        _clear_pid()
        raise RuntimeError(
            "The node process exited right after starting. "
            f"Check the log for details: {log_file()}"
        )


def _stop_daemon() -> bool:
    """Stop the daemon if it's running. Returns True if we actually stopped
    something, False if it wasn't running."""
    pid = _read_pid()
    if pid is None or not _pid_alive(pid):
        _clear_pid()
        return False

    _terminate(pid)

    # Wait a few seconds for a graceful shutdown, then force it.
    for _ in range(30):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        _terminate(pid, force=True)

    _clear_pid()
    return True


def _daemon_uptime_seconds() -> int | None:
    """Approximate uptime from when we wrote the pid file at launch."""
    path = pid_file()
    if not path.exists():
        return None
    try:
        started = path.stat().st_mtime
    except OSError:
        return None
    return max(0, int(time.time() - started))


_SYSTEMD_UNIT_NAME = "tandem-node.service"
_LAUNCHD_LABEL = "org.tandem.node"


def _service_kind() -> str | None:
    """Which OS service manager we can use here, if any."""
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return "systemd"
    if sys.platform == "darwin":
        return "launchd"
    return None


def service_supported() -> bool:
    return _service_kind() is not None


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT_NAME


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


def _systemd_is_active() -> bool:
    result = _systemctl("is-active", _SYSTEMD_UNIT_NAME)
    return result.stdout.strip() == "active"


def _launchd_is_loaded() -> bool:
    result = subprocess.run(
        ["launchctl", "list", _LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _service_environment_lines(server_url: str) -> list[tuple[str, str]]:
    """The env vars the service needs to run the node the same way the CLI does."""
    pairs = [
        ("TANDEM_SERVER_URL", server_url),
        ("TANDEM_NODE_STATE_PATH", str(state_file())),
        ("TANDEM_PRIVATE_KEY_PATH", str(private_key_file())),
    ]
    token = resolve_registration_token()
    if token:
        pairs.append(("TANDEM_NODE_REGISTRATION_TOKEN", token))
    return pairs


def _write_systemd_unit(binary: Path, server_url: str) -> None:
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)

    env_lines = "\n".join(
        f'Environment="{key}={value}"' for key, value in _service_environment_lines(server_url)
    )

    unit = f"""[Unit]
Description=Tandem compute node
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart="{binary}"
{env_lines}
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit, encoding="utf-8")


def _write_launchd_plist(binary: Path, server_url: str) -> None:
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    env_entries = "".join(
        f"    <key>{key}</key><string>{value}</string>\n"
        for key, value in _service_environment_lines(server_url)
    )

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{binary}</string></array>
  <key>EnvironmentVariables</key>
  <dict>
{env_entries}  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log_file()}</string>
  <key>StandardErrorPath</key><string>{log_file()}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")


def _enable_systemd(binary: Path, server_url: str) -> list[str]:
    """Install + start the systemd user service. Returns human-readable notes."""
    _write_systemd_unit(binary, server_url)
    _systemctl("daemon-reload")
    result = _systemctl("enable", "--now", _SYSTEMD_UNIT_NAME)
    notes: list[str] = []
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl could not enable the service: {result.stderr.strip() or result.stdout.strip()}"
        )

    # Lingering is what lets a user service keep running (and start on boot)
    # without the user being logged in -- the real "24/7" bit.
    linger = subprocess.run(
        ["loginctl", "enable-linger", os.environ.get("USER", "")],
        capture_output=True,
        text=True,
    )
    if linger.returncode == 0:
        notes.append("Enabled lingering so the node keeps running across reboots.")
    else:
        notes.append(
            "Could not enable lingering automatically. For the node to run before "
            "you log in, run:  sudo loginctl enable-linger $USER"
        )
    return notes


def _enable_launchd(binary: Path, server_url: str) -> list[str]:
    _write_launchd_plist(binary, server_url)
    plist_path = _launchd_plist_path()
    # Unload any previous copy first so a re-enable picks up changes cleanly.
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"launchctl could not load the service: {result.stderr.strip() or result.stdout.strip()}"
        )
    return ["Loaded the launchd agent; it will start on login and restart if it crashes."]


def _disable_systemd() -> None:
    _systemctl("disable", "--now", _SYSTEMD_UNIT_NAME)
    try:
        _systemd_unit_path().unlink()
    except FileNotFoundError:
        pass
    _systemctl("daemon-reload")


def _disable_launchd() -> None:
    plist_path = _launchd_plist_path()
    subprocess.run(["launchctl", "unload", "-w", str(plist_path)], capture_output=True, text=True)
    try:
        plist_path.unlink()
    except FileNotFoundError:
        pass


def active_backend() -> str:
    """Which backend is in charge right now. A service only counts as active once
    its unit/plist file has been written by `tandem node enable`; otherwise we're
    in plain-daemon mode."""
    kind = _service_kind()
    if kind == "systemd" and _systemd_unit_path().exists():
        return "systemd"
    if kind == "launchd" and _launchd_plist_path().exists():
        return "launchd"
    return "daemon"


def node_is_running() -> bool:
    """The single question the lock and status both ask."""
    backend = active_backend()
    if backend == "systemd":
        return _systemd_is_active()
    if backend == "launchd":
        return _launchd_is_loaded()
    return _daemon_running()


def start_node(server_url: str) -> None:
    """Start the node using whichever backend is active."""
    backend = active_backend()
    if backend == "systemd":
        _systemctl("start", _SYSTEMD_UNIT_NAME)
        return
    if backend == "launchd":
        subprocess.run(
            ["launchctl", "load", "-w", str(_launchd_plist_path())],
            capture_output=True,
            text=True,
        )
        return
    _start_daemon(server_url)


def stop_node() -> bool:
    """Stop the node. Returns True if something was actually running."""
    backend = active_backend()
    if backend == "systemd":
        was_running = _systemd_is_active()
        _systemctl("stop", _SYSTEMD_UNIT_NAME)
        return was_running
    if backend == "launchd":
        was_running = _launchd_is_loaded()
        subprocess.run(
            ["launchctl", "unload", str(_launchd_plist_path())],
            capture_output=True,
            text=True,
        )
        return was_running
    return _stop_daemon()


def enable_service(server_url: str) -> list[str]:
    """Turn on the OS-service backend so the node runs 24/7 across reboots."""
    kind = _service_kind()
    if kind is None:
        raise RuntimeError(
            "No supported OS service manager here (systemd or launchd). "
            "Use `tandem node start` to run it in the background for this session."
        )
    binary = find_node_binary()
    if binary is None:
        raise RuntimeError(_missing_binary_message())

    # If a plain daemon is running, hand off cleanly to the service.
    if _daemon_running():
        _stop_daemon()

    if kind == "systemd":
        return _enable_systemd(binary, server_url)
    return _enable_launchd(binary, server_url)


def disable_service() -> str:
    """Turn the OS service back off, reverting to plain-daemon mode."""
    backend = active_backend()
    if backend == "systemd":
        _disable_systemd()
        return "systemd"
    if backend == "launchd":
        _disable_launchd()
        return "launchd"
    return "none"


def get_status() -> NodeStatus:
    """A full snapshot for `tandem status` / `tandem node status`."""
    backend = active_backend()
    running = node_is_running()
    identity = load_identity() or {}

    pid = _read_pid() if backend == "daemon" else None
    uptime = _daemon_uptime_seconds() if (backend == "daemon" and running) else None

    registered_at = identity.get("registered_at")
    if not isinstance(registered_at, int):
        registered_at = None

    return NodeStatus(
        running=running,
        backend=backend if (running or is_registered()) else "none",
        pid=pid,
        node_id=identity.get("node_id"),
        server_url=identity.get("server_url"),
        registered_at=registered_at,
        uptime_seconds=uptime,
    )


def tail_log(lines: int = 40) -> str:
    """Return the last few lines of the node log, for `tandem node logs`."""
    path = log_file()
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])
