from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .analysis import AnalysisReport, Diagnostic, analyze_tasks
from .app_config import ProjectConfig, load_project_config
from .discovery import DiscoveredProject, discover_project
from .manifest import build_manifest
from .wasm import build_wasm


class AnalysisFailure(RuntimeError):
    """Raised when static validation fails in strict build mode."""

    def __init__(self, report: AnalysisReport) -> None:
        self.report = report
        super().__init__(
            f"Build aborted due to {report.error_count} analysis error(s)."
        )


@dataclass(frozen=True)
class BuildResult:
    config: ProjectConfig
    output_dir: Path
    manifest_path: Path
    analysis_path: Path
    sdk_bridge_path: Path
    wasm_paths: tuple[Path, ...]
    task_count: int
    diagnostics: tuple[Diagnostic, ...]


def inspect_project(
    config_path: str | Path,
) -> tuple[ProjectConfig, DiscoveredProject, AnalysisReport]:
    """Load project config, discover tasks, and run analysis."""

    config = load_project_config(config_path)
    discovered = discover_project(config)
    if not discovered.tasks:
        raise RuntimeError(
            f"No Tandem tasks were discovered in {config.entry_path}. Add decorators like `@tandem.compute`."
        )

    report = analyze_tasks(discovered.module, discovered.tasks)
    return config, discovered, report


@contextlib.contextmanager
def _compile_source_dir(config: ProjectConfig) -> Iterator[Path]:
    """Yield the directory to hand the compiler as its Python source path.

    The compile engine drives componentize-py, which resolves `import tandem`
    from whatever happens to be on the ambient Python path. That means a stale
    or mismatched `tandem` installed elsewhere on the machine can shadow the SDK
    this CLI ships with, and the task gets frozen against the wrong SDK. To keep
    builds hermetic we stage the user's sources together with the bundled SDK in
    a throwaway directory and compile from there: the compiler searches this
    directory before the ambient environment, so the bundled copy always wins.

    When no bundled SDK path is known (e.g. a custom dev checkout without one),
    we fall back to compiling the sources in place -- the previous behaviour.
    """
    source_dir = config.entry_path.parent
    package = config.sdk_import_name
    sdk_path = config.sdk_path
    if sdk_path is None or not (sdk_path / package).is_dir():
        yield source_dir
        return

    # Skip build/VCS/env noise, and whatever the output dir is if it lives under
    # the sources (so we never copy a previous build back into the next one).
    ignore_names = [
        "__pycache__", "*.pyc", "*.pyo", ".git", "*.egg-info",
        ".tandem", ".tandem_build", ".venv", "venv", "node_modules",
    ]
    try:
        output_rel = config.output_dir.relative_to(source_dir)
    except ValueError:
        output_rel = None
    if output_rel is not None and output_rel.parts:
        ignore_names.append(output_rel.parts[0])

    staging = Path(tempfile.mkdtemp(prefix="tandem-build-"))
    try:
        shutil.copytree(
            source_dir,
            staging,
            ignore=shutil.ignore_patterns(*ignore_names),
            dirs_exist_ok=True,
        )
        # Drop the bundled SDK package in alongside the sources so `import
        # <package>` resolves here first, ahead of anything installed globally.
        shutil.copytree(sdk_path / package, staging / package, dirs_exist_ok=True)
        yield staging
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_project(config_path: str | Path, *, strict: bool = True) -> BuildResult:
    """Build discovered tasks into compiled WASM components plus a manifest."""

    config, discovered, report = inspect_project(config_path)
    if strict and report.has_errors:
        raise AnalysisFailure(report)

    if config.output_dir.exists():
        shutil.rmtree(config.output_dir)

    tasks_dir = config.output_dir / "tasks"
    tandem_dir = config.output_dir / ".tandem"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    tandem_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(config, discovered)
    manifest_path = tandem_dir / "manifest.json"
    analysis_path = tandem_dir / "analysis.json"
    sdk_bridge_path = tandem_dir / "sdk-bridge.json"

    entry_lookup = {entry["name"]: entry for entry in manifest["tasks"]}
    wasm_paths: list[Path] = []

    # Compile every task against a staged copy of the sources that carries the
    # bundled SDK, so the frozen component never picks up a stray global install.
    with _compile_source_dir(config) as compile_source:
        for export_name, task in sorted(discovered.tasks.items()):
            manifest_entry = entry_lookup[export_name]
            wasm_path = config.output_dir / manifest_entry["wasm"]
            wasm_path.parent.mkdir(parents=True, exist_ok=True)
            # The compile engine imports the entry module and grabs the marked
            # function by the name it's exported under, so that's what we hand it.
            wasm_path.write_bytes(
                build_wasm(
                    source_dir=compile_source,
                    entry_module=config.entry_path.stem,
                    entry_function=export_name,
                )
            )
            wasm_paths.append(wasm_path)

    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    analysis_path.write_text(
        json.dumps(
            {
                "diagnostics": [
                    diagnostic.as_dict() for diagnostic in report.diagnostics
                ]
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    sdk_bridge_path.write_text(
        json.dumps(discovered.sdk_descriptor.as_dict(), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    return BuildResult(
        config=config,
        output_dir=config.output_dir,
        manifest_path=manifest_path,
        analysis_path=analysis_path,
        sdk_bridge_path=sdk_bridge_path,
        wasm_paths=tuple(wasm_paths),
        task_count=len(discovered.tasks),
        diagnostics=report.diagnostics,
    )


def clean_project(config_path: str | Path) -> Path:
    """Remove generated build artifacts for a project."""

    config = load_project_config(config_path)
    if config.output_dir.exists():
        shutil.rmtree(config.output_dir)
    return config.output_dir
