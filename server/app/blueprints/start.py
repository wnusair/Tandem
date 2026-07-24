"""Queue Tandem jobs for node execution.

The original flow accepted cloudpickle task blobs directly from the CLI. The
WASM build path keeps the same job/token model, but the server now reads the
CLI manifest and fans `.wasm` artifacts out to healthy nodes using the SDK's
execution markers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models import Deployment
from app.utils import verify
from app.utils.auth import ensure_deployment_access, require_user_api_key
from app.utils.task_queue import (
    compare_token,
    create_job,
    create_task,
    get_available_nodes,
    get_job,
    get_job_results,
    refresh_job_status,
    select_least_loaded_node,
)
from app.utils.toml_reader import extract_name, get_relevant, parse_toml_string
from flask import Blueprint, jsonify, request
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

start_bp = Blueprint("start", __name__)


def _require_job_access(job_id: str):
    api_client, error = require_user_api_key()
    if error:
        return None, None, error
    assert api_client is not None

    job = get_job(job_id)
    if not job:
        return None, None, (jsonify({"error": "Unknown job id"}), 404)

    provided = request.headers.get("X-Job-Token") or request.args.get("token")
    if not compare_token(job.get("job_token"), provided):
        return None, None, (jsonify({"error": "Invalid or missing job token"}), 403)

    pid = (job.get("pid") or "").strip()
    deployment = Deployment.query.filter_by(pid=pid).first()
    if not deployment:
        return None, None, (jsonify({"error": "Unknown deployment pid"}), 404)

    deployment_error = ensure_deployment_access(api_client, deployment)
    if deployment_error:
        return None, None, deployment_error

    return job, deployment, None


def _coerce_non_negative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None

    if parsed < 0:
        return None
    return parsed


def _build_job_response(
    *,
    pid: str,
    name: str,
    job_id: str,
    job_token: str,
) -> tuple[Any, int]:
    summary = refresh_job_status(job_id)
    base_url = request.host_url.rstrip("/")

    task_ids = [
        task.get("tid")
        for task in summary["tasks"]
        if isinstance(task, dict) and task.get("tid")
    ]

    return (
        jsonify(
            {
                "message": "Tasks queued successfully",
                "pid": pid,
                "job_id": job_id,
                "job_token": job_token,
                "name": name,
                "task_ids": task_ids,
                "counts": summary["counts"],
                "status": summary["status"],
                "status_url": f"{base_url}/start/{job_id}",
                "results_url": f"{base_url}/start/{job_id}/results",
            }
        ),
        202,
    )


def _queue_planned_tasks(
    *,
    pid: str,
    name: str,
    metadata: dict[str, Any],
    planned_tasks: list[dict[str, Any]],
    available_nodes: list[str],
) -> tuple[Any, int]:
    job_info = create_job(
        pid=pid,
        name=name,
        metadata=metadata,
        total_tasks=len(planned_tasks),
    )
    job_id = job_info["job_id"]
    job_token = job_info["job_token"]

    # Some of these get run on several nodes at once so we can compare the
    # answers. Doing it here rather than in the planners means both the wasm and
    # cloudpickle paths get checked without either of them knowing about it.
    planned_tasks = verify.plan_verification_replicas(planned_tasks, available_nodes)

    for planned_task in planned_tasks:
        create_task(
            job_id=job_id,
            pid=pid,
            name=name,
            filename=planned_task["filename"],
            payload=planned_task["payload"],
            assigned_node=planned_task["assigned_node"],
            runtime=planned_task.get("runtime", "cloudpickle"),
            task_name=planned_task.get("task_name", ""),
            timeout_ms=planned_task.get("timeout_ms"),
            shard_index=planned_task.get("shard_index"),
            shard_total=planned_task.get("shard_total"),
            verify_group=planned_task.get("verify_group", ""),
            verify_replica=planned_task.get("verify_replica", False),
        )

    return _build_job_response(
        pid=pid,
        name=name,
        job_id=job_id,
        job_token=job_token,
    )


def _plan_cloudpickle_tasks(
    *,
    pickle_files: list[FileStorage],
    available_nodes: list[str],
) -> list[dict[str, Any]]:
    if not pickle_files:
        raise ValueError("No cloudpickle files provided")

    node_load: dict[str, int] = {}
    planned_tasks: list[dict[str, Any]] = []

    for pickle_file in pickle_files:
        filename = secure_filename(pickle_file.filename or "") or "task.pkl"
        payload = pickle_file.read()
        if not payload:
            raise ValueError(f"Cloudpickle file `{filename}` was empty")

        planned_tasks.append(
            {
                "filename": filename,
                "payload": payload,
                "assigned_node": select_least_loaded_node(available_nodes, node_load),
                "runtime": "cloudpickle",
                "task_name": Path(filename).stem,
            }
        )

    return planned_tasks


def _read_manifest_upload(manifest_file: FileStorage) -> dict[str, Any]:
    raw_manifest = manifest_file.read()
    if not raw_manifest:
        raise ValueError("Manifest file was empty")

    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Manifest file was not valid JSON: {exc.msg}") from exc

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Manifest did not include any tasks")

    return manifest


def _collect_wasm_uploads(wasm_files: list[FileStorage]) -> dict[str, bytes]:
    if not wasm_files:
        raise ValueError("No wasm files provided")

    uploads: dict[str, bytes] = {}

    for wasm_file in wasm_files:
        filename = secure_filename(Path(wasm_file.filename or "").name)
        if not filename:
            raise ValueError("One uploaded wasm file did not have a usable filename")
        if not filename.endswith(".wasm"):
            raise ValueError(f"Uploaded file `{filename}` is not a `.wasm` artifact")
        if filename in uploads:
            raise ValueError(f"Duplicate wasm upload for `{filename}`")

        payload = wasm_file.read()
        if not payload:
            raise ValueError(f"WASM artifact `{filename}` was empty")

        uploads[filename] = payload

    return uploads


def _planned_wasm_shards(task_entry: dict[str, Any], available_nodes: list[str]) -> int:
    split_hint = task_entry.get("split")
    if not isinstance(split_hint, dict):
        return 1

    strategy = str(split_hint.get("strategy") or "").strip().lower()
    if strategy != "data_parallel":
        return 1

    # The SDK writes the split hint as `chunk` (from the user's
    # tandem.split(fn, chunk=N)); that's the fan-out cap the planner uses.
    raw_cap = split_hint.get("chunk")
    shard_cap = _coerce_non_negative_int(raw_cap) or 1
    if shard_cap < 2 or len(available_nodes) < 2:
        return 1

    # No input sharder exists yet, so keep the fan-out tied to live worker count.
    return min(len(available_nodes), shard_cap)


def _plan_wasm_tasks(
    *,
    manifest: dict[str, Any],
    wasm_files: list[FileStorage],
    available_nodes: list[str],
) -> list[dict[str, Any]]:
    uploads = _collect_wasm_uploads(wasm_files)
    node_load: dict[str, int] = {}
    planned_tasks: list[dict[str, Any]] = []
    used_uploads: set[str] = set()

    for raw_entry in manifest.get("tasks", []):
        if not isinstance(raw_entry, dict):
            raise ValueError("Manifest task entries must be JSON objects")

        task_name = str(raw_entry.get("name") or "").strip()
        wasm_path = str(raw_entry.get("wasm") or "").strip()
        if not task_name or not wasm_path:
            raise ValueError("Each manifest task needs both `name` and `wasm`")

        upload_name = secure_filename(Path(wasm_path).name)
        if not upload_name:
            raise ValueError(f"Manifest task `{task_name}` had an invalid wasm path")

        payload = uploads.get(upload_name)
        if payload is None:
            raise ValueError(
                f"Manifest task `{task_name}` expected upload `{upload_name}`, but it was missing"
            )

        used_uploads.add(upload_name)
        shard_total = _planned_wasm_shards(raw_entry, available_nodes)
        timeout_ms = _coerce_non_negative_int(raw_entry.get("timeout_ms"))

        for shard_index in range(shard_total):
            planned_task: dict[str, Any] = {
                "filename": upload_name,
                "payload": payload,
                "assigned_node": select_least_loaded_node(available_nodes, node_load),
                "runtime": "wasm",
                "task_name": task_name,
                "timeout_ms": timeout_ms,
            }
            if shard_total > 1:
                planned_task["shard_index"] = shard_index
                planned_task["shard_total"] = shard_total
            planned_tasks.append(planned_task)

    unused_uploads = sorted(set(uploads) - used_uploads)
    if unused_uploads:
        joined = ", ".join(unused_uploads)
        raise ValueError(
            f"Uploaded wasm files were not referenced by the manifest: {joined}"
        )

    if not planned_tasks:
        raise ValueError("Manifest did not produce any runnable wasm tasks")

    return planned_tasks


@start_bp.route("/", methods=["POST"])
def start():
    """Queue one job made up of cloudpickle tasks or CLI-built `.wasm` artifacts."""

    if "toml_file" not in request.files:
        return jsonify({"error": "Missing TOML config file"}), 400

    pid = (request.form.get("pid") or "").strip()
    if not pid:
        return jsonify({"error": "Missing deployment pid"}), 400

    api_client, error = require_user_api_key()
    if error:
        return error
    assert api_client is not None

    from app.utils import quota
    under_quota, info = quota.check_quota(api_client.api_key)
    if not under_quota:
        return jsonify({
            "error": "Instruction quota exceeded",
            "used": info["used"],
            "limit": info["limit"],
            "remaining": info["remaining"]
        }), 429

    deployment = Deployment.query.filter_by(pid=pid).first()
    if not deployment:
        return jsonify({"error": "Unknown deployment pid"}), 404

    deployment_error = ensure_deployment_access(api_client, deployment)
    if deployment_error:
        return deployment_error

    toml_file = request.files["toml_file"]
    toml_bytes = toml_file.read()
    parsed = parse_toml_string(toml_bytes)
    name = extract_name(parsed) or deployment.name
    relevant = get_relevant(parsed)

    if deployment.name and name and deployment.name != name:
        return jsonify(
            {
                "error": "Deployment PID does not match uploaded app config",
                "expected_name": deployment.name,
                "received_name": name,
            }
        ), 400

    try:
        if "manifest_file" in request.files or request.files.getlist("wasm_files"):
            if "manifest_file" not in request.files:
                return jsonify({"error": "Missing manifest file for wasm start"}), 400

            available_nodes = get_available_nodes(required_runtime="wasm")
            if not available_nodes:
                return jsonify(
                    {"error": "No healthy wasm-capable nodes found in Redis"}
                ), 503

            manifest = _read_manifest_upload(request.files["manifest_file"])
            planned_tasks = _plan_wasm_tasks(
                manifest=manifest,
                wasm_files=request.files.getlist("wasm_files"),
                available_nodes=available_nodes,
            )
        else:
            available_nodes = get_available_nodes()
            if not available_nodes:
                return jsonify({"error": "No healthy nodes found in Redis"}), 503

            planned_tasks = _plan_cloudpickle_tasks(
                pickle_files=request.files.getlist("pickle_files"),
                available_nodes=available_nodes,
            )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return _queue_planned_tasks(
        pid=pid,
        name=name,
        metadata=relevant,
        planned_tasks=planned_tasks,
        available_nodes=available_nodes,
    )


@start_bp.route("/<job_id>", methods=["GET"])
def job_status(job_id: str):
    job, _deployment, error = _require_job_access(job_id)
    if error:
        return error
    assert job is not None

    summary = refresh_job_status(job_id)
    metadata_json = job.get("metadata_json") or "{}"
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        metadata = {}

    return jsonify(
        {
            "job_id": job_id,
            "pid": job.get("pid") or "",
            "name": job.get("name") or "",
            "status": summary["status"],
            "done": summary["done"],
            "counts": summary["counts"],
            "tasks": summary["tasks"],
            "metadata": metadata,
            "created_at": job.get("created_at") or "",
            "updated_at": job.get("updated_at") or "",
        }
    )


@start_bp.route("/<job_id>/results", methods=["GET"])
def job_results(job_id: str):
    job, _deployment, error = _require_job_access(job_id)
    if error:
        return error
    assert job is not None

    summary = refresh_job_status(job_id)
    if not summary["done"]:
        return (
            jsonify(
                {
                    "job_id": job_id,
                    "pid": job.get("pid") or "",
                    "status": summary["status"],
                    "done": False,
                    "counts": summary["counts"],
                    "tasks": summary["tasks"],
                }
            ),
            202,
        )

    return jsonify(
        {
            "job_id": job_id,
            "pid": job.get("pid") or "",
            "name": job.get("name") or "",
            "status": summary["status"],
            "done": True,
            "counts": summary["counts"],
            "results": get_job_results(job_id),
        }
    )
