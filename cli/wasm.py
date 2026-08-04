from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _resolve_compile_bin() -> str:
    """Find the `tandem-compile` engine binary.

    An explicit env override wins; otherwise we look where install.sh /
    install.bat put it (next to the node binary), then fall back to PATH and
    finally the bare name so the subprocess call raises a clear "not found"
    error.
    """
    override = os.environ.get("TANDEM_COMPILE_BIN")
    if override:
        return override
    binary_name = "tandem-compile.exe" if os.name == "nt" else "tandem-compile"
    home_bin = Path.home() / ".tandem" / "bin" / binary_name
    if home_bin.exists():
        return str(home_bin)
    return shutil.which(binary_name) or binary_name


def _resolve_componentize_py() -> str:
    """Find the `componentize-py` toolchain.

    It's a dependency of the CLI, so it installs into the same environment as the
    CLI. That means the most reliable place to look is right next to the Python
    that's running us, before falling back to PATH.
    """
    override = os.environ.get("TANDEM_COMPONENTIZE_PY")
    if override:
        return override
    binary_name = "componentize-py.exe" if os.name == "nt" else "componentize-py"
    sibling = Path(sys.executable).parent / binary_name
    if sibling.exists():
        return str(sibling)
    return shutil.which(binary_name) or binary_name


def _resolve_wit_dir() -> str:
    """Find the directory that holds Tandem's `task.wit` contract."""
    override = os.environ.get("TANDEM_WIT_DIR")
    if override:
        return override

    # Once packaged, the WIT ships next to this module under _bundled/wit.
    bundled = Path(__file__).resolve().parent / "_bundled" / "wit"
    if (bundled / "task.wit").exists():
        return str(bundled)

    raise RuntimeError(
        "Could not find Tandem's WIT directory. Set TANDEM_WIT_DIR to the folder "
        "that contains task.wit."
    )


def _resolve_cache_dir() -> str:
    """Where compiled components get cached between builds."""
    override = os.environ.get("TANDEM_COMPILE_CACHE")
    if override:
        return override
    cache = Path.home() / ".tandem" / "compile-cache"
    cache.mkdir(parents=True, exist_ok=True)
    return str(cache)


def build_wasm(
    *,
    source_dir: Path | str,
    entry_module: str,
    entry_function: str,
    options: dict[str, Any] | None = None,
) -> bytes:
    """Compile a marked Python function into a WASM component.

    The real work happens in the Rust compile engine (the `tandem-compile`
    binary, which drives componentize-py). This function just resolves the tools,
    shells out, and hands back the component bytes.
    """
    compile_bin = _resolve_compile_bin()
    componentize_py = _resolve_componentize_py()
    wit_dir = _resolve_wit_dir()
    cache_dir = _resolve_cache_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "task.wasm")
        command = [
            compile_bin,
            "--source", str(source_dir),
            "--entry-module", entry_module,
            "--entry-function", entry_function,
            "--language", "python",
            "--wit-dir", wit_dir,
            "--componentize-py", componentize_py,
            "--cache", cache_dir,
            "--out", out_path,
        ]
        for key, value in (options or {}).items():
            command.extend(["--option", f"{key}={value}"])

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not find the Tandem compiler `{compile_bin}`. It's built "
                "and installed alongside the CLI; set TANDEM_COMPILE_BIN to point "
                "at it if it lives somewhere custom."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr_text = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise RuntimeError(
                f"Failed to compile `{entry_module}.{entry_function}` to a WASM "
                f"component:\n{stderr_text}"
            ) from exc

        with open(out_path, "rb") as handle:
            return handle.read()
