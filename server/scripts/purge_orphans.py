#!/usr/bin/env python3
"""
Maintenance script to clean up orphaned task blobs and result files.

Walks the `runtime/tasks/` and `runtime/results/` directories, checks if the
corresponding task exists in Redis, and removes the file if the task is gone
or has been in a terminal state for longer than the retention window.
"""

import argparse
import pathlib
import sys
import time

# Add the server root to sys.path to import the app context
server_dir = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(server_dir))

from app import create_app
from app.utils.task_queue import get_task


def parse_args():
    parser = argparse.ArgumentParser(description="Purge orphaned task and result files.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete files (default is dry-run)",
    )
    parser.add_argument(
        "--retention-hours",
        type=float,
        default=1.0,
        help="Hours to keep files for terminal tasks (default: 1.0)",
    )
    return parser.parse_args()


def purge(execute: bool, retention_seconds: float):
    app = create_app()
    with app.app_context():
        runtime_dir = pathlib.Path(app.root_path).resolve().parent / "runtime"
        if not runtime_dir.exists():
            print(f"Runtime directory {runtime_dir} does not exist. Nothing to do.")
            return

        now = time.time()
        tasks_dir = runtime_dir / "tasks"
        results_dir = runtime_dir / "results"

        deleted_count = 0
        deleted_bytes = 0
        scanned_count = 0

        for target_dir in (tasks_dir, results_dir):
            if not target_dir.exists():
                continue

            for filepath in target_dir.rglob("*"):
                if not filepath.is_file():
                    continue

                scanned_count += 1
                # Path format: runtime/tasks/job_id/tid.ext
                tid_with_ext = filepath.name
                tid = tid_with_ext.split(".")[0]

                task = get_task(tid)
                should_delete = False

                if not task:
                    should_delete = True
                else:
                    status = task.get("status")
                    if status in {"completed", "failed"}:
                        updated_at = task.get("updated_at")
                        if updated_at:
                            try:
                                age = now - float(updated_at)
                                if age > retention_seconds:
                                    should_delete = True
                            except ValueError:
                                should_delete = True
                        else:
                            should_delete = True

                if should_delete:
                    size = filepath.stat().st_size
                    if execute:
                        print(f"Deleting {filepath}")
                        filepath.unlink()
                    else:
                        print(f"[Dry Run] Would delete {filepath}")
                    
                    deleted_count += 1
                    deleted_bytes += size

        # Cleanup empty directories
        if execute:
            for target_dir in (tasks_dir, results_dir):
                if not target_dir.exists():
                    continue
                for dirpath in sorted(target_dir.rglob("*"), reverse=True):
                    if dirpath.is_dir() and not any(dirpath.iterdir()):
                        dirpath.rmdir()

        mode = "Executed" if execute else "Dry Run"
        print(f"\n{mode} summary:")
        print(f"Scanned files: {scanned_count}")
        print(f"Deleted files: {deleted_count}")
        print(f"Space freed:   {deleted_bytes / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    args = parse_args()
    purge(args.execute, args.retention_hours * 3600)
