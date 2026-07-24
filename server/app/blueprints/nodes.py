from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import uuid

from cryptography.hazmat.primitives import serialization

from app.extensions import db, redis_client
from app.models import Deployment, NodePublicKey, TaskEncryptionKey
from app.utils import quota, zkp
from app.utils.auth import get_api_client
from app.utils.task_queue import (
    TASK_LEASE_SECONDS,
    claim_task_for_node,
    compare_token,
    complete_task,
    extend_task_lease,
    fail_task,
    get_node,
    get_task,
    requeue_task,
)
from flask import Blueprint, after_this_request, current_app, jsonify, request, send_file

logger = logging.getLogger(__name__)

nodes_bp = Blueprint("nodes", __name__)


def _extract_node_id() -> str:
    header_node_id = (request.headers.get("X-Node-Id") or "").strip()
    if header_node_id:
        return header_node_id

    if request.is_json:
        data = request.get_json(silent=True) or {}
        return (data.get("node_id") or "").strip()

    return ""


def _extract_bearer_token() -> str:
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.startswith("Bearer "):
        return ""
    return auth_header.split(" ", 1)[1].strip()


def _require_node_auth():
    node_id = _extract_node_id()
    if not node_id:
        return None, None, (jsonify({"error": "Missing node_id"}), 400)

    token = _extract_bearer_token()
    if not token:
        return None, None, (jsonify({"error": "Missing bearer token"}), 401)

    node = get_node(node_id)
    if not node:
        return None, None, (jsonify({"error": "Unknown node_id"}), 404)

    if not compare_token(node.get("node_token"), token):
        return None, None, (jsonify({"error": "Invalid node token"}), 403)

    return node_id, node, None


def _resolve_registration_identity():
    """Decide if the caller may register a node, and who owns it.

    A valid user API key is enough (and tells us the owner); otherwise we fall
    back to the shared registration token for headless nodes. Returns
    (api_client, error): on success error is None, on failure it's a Flask
    (response, status) tuple.
    """
    provided = _extract_bearer_token()

    if provided:
        api_client = get_api_client(provided)
        if api_client is not None:
            return api_client, None

    expected = (current_app.config.get("NODE_REGISTRATION_TOKEN") or "").strip()
    if not expected:
        return None, None
    if not provided:
        return None, (jsonify({"error": "Missing node registration bearer token"}), 401)
    if not compare_token(expected, provided):
        return None, (jsonify({"error": "Invalid node registration token"}), 403)
    return None, None


def _as_capability_flag(value: object) -> str:
    return "1" if bool(value) else "0"


def _maybe_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_mimetype(task: dict[str, str]) -> str:
    if (task.get("runtime") or "").strip() == "wasm":
        return "application/wasm"
    return "application/octet-stream"


@nodes_bp.route("/health", methods=["POST"])
def health():
    node_id, _node, error = _require_node_auth()
    if error:
        return error
    assert node_id is not None

    data = request.get_json(silent=True) or {}
    timestamp = str(time.time())
    mapping = {"last_seen": timestamp}

    for field in ("latency", "download", "upload"):
        value = data.get(field)
        if value is not None:
            mapping[field] = str(value)

    redis_client.hset(f"node:{node_id}", mapping=mapping)
    redis_client.sadd("nodes", node_id)
    extend_task_lease(node_id)

    return jsonify({"status": "Alive"}), 200


@nodes_bp.route("/register", methods=["POST"])
def register():
    owner, registration_error = _resolve_registration_identity()
    if registration_error is not None:
        return registration_error

    data = request.get_json(silent=True) or {}
    node_id = f"node_{uuid.uuid4().hex[:12]}"
    node_token = secrets.token_urlsafe(32)
    timestamp = str(time.time())

    metrics = {
        "node_token": node_token,
        "last_seen": timestamp,
        "supports_wasm": _as_capability_flag(data.get("supports_wasm")),
    }

    # Nodes that registered with an API key remember their owner; token-based
    # ones stay anonymous.
    if owner is not None:
        metrics["owner_user_id"] = str(owner.user_id)

    for field in ("latency", "download", "upload", "cpu_cores", "cpu_name", "cpu_mhz", "memory_mb"):
        value = data.get(field)
        if value is not None:
            metrics[field] = str(value)

    redis_client.hset(f"node:{node_id}", mapping=metrics)
    redis_client.sadd("nodes", node_id)

    # Accept optional RSA public key for task payload encryption
    rsa_pem = (data.get("rsa_public_key_pem") or "").strip()
    if rsa_pem:
        try:
            serialization.load_pem_public_key(rsa_pem.encode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid RSA public key PEM"}), 400

        # Upsert: delete any existing row for this node_id, then insert
        NodePublicKey.query.filter_by(node_id=node_id).delete()
        db.session.add(NodePublicKey(node_id=node_id, rsa_public_key_pem=rsa_pem))
        db.session.commit()

    return (
        jsonify(
            {
                "status": "Registered",
                "node_id": node_id,
                "node_token": node_token,
            }
        ),
        201,
    )


@nodes_bp.route("/tasks/claim", methods=["POST"])
def claim_task():
    node_id, _node, error = _require_node_auth()
    if error:
        return error
    assert node_id is not None

    task = claim_task_for_node(node_id)
    if not task:
        return ("", 204)

    tid = task.get("tid") or ""
    download_token = task.get("download_token") or ""
    base_url = request.host_url.rstrip("/")

    response: dict[str, object] = {
        "tid": tid,
        "job_id": task.get("job_id") or "",
        "task_name": task.get("task_name") or "",
        "filename": task.get("filename") or "",
        "runtime": task.get("runtime") or "cloudpickle",
        "claim_token": task.get("claim_token") or "",
        "download_url": f"{base_url}/nodes/tasks/{tid}/download/{download_token}",
    }

    timeout_ms = _maybe_int(task.get("timeout_ms"))
    shard_index = _maybe_int(task.get("shard_index"))
    shard_total = _maybe_int(task.get("shard_total"))
    if timeout_ms is not None:
        response["timeout_ms"] = timeout_ms
    if shard_index is not None and shard_total is not None:
        response["shard_index"] = shard_index
        response["shard_total"] = shard_total

    return jsonify(response)


@nodes_bp.route("/tasks/<tid>/download/<download_token>", methods=["GET"])
def download_task_blob(tid: str, download_token: str):
    node_id, _node, error = _require_node_auth()
    if error:
        return error

    task = get_task(tid)
    if not task:
        return jsonify({"error": "Unknown task id"}), 404

    if task.get("assigned_node") != node_id:
        return jsonify({"error": "Task is assigned to another node"}), 403

    if not compare_token(task.get("download_token"), download_token):
        return jsonify({"error": "Invalid download token"}), 403

    blob_path = task.get("blob_path")
    if not blob_path or not os.path.exists(blob_path):
        return jsonify({"error": "Task payload is missing"}), 404

    timestamp = str(time.time())
    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": "running",
            "updated_at": timestamp,
            "lease_expires_at": str(time.time() + TASK_LEASE_SECONDS),
        },
    )

    # Attach encryption key headers if available
    enc_key_row = TaskEncryptionKey.query.filter_by(tid=tid).first()

    response = send_file(
        blob_path,
        mimetype=_task_mimetype(task),
        as_attachment=True,
        download_name=task.get("filename") or f"{tid}.bin",
    )

    if enc_key_row is not None:
        response.headers["X-Task-Dek-Encrypted"] = enc_key_row.encrypted_dek_b64
        response.headers["X-Task-IV"] = enc_key_row.iv_b64

        @after_this_request
        def _cleanup_enc_key(resp):
            try:
                TaskEncryptionKey.query.filter_by(tid=tid).delete()
                db.session.commit()
            except Exception:
                logger.warning("Failed to delete TaskEncryptionKey for %s", tid, exc_info=True)
            return resp

    return response


@nodes_bp.route("/tasks/<tid>/result", methods=["POST"])
def submit_task_result(tid: str):
    node_id, _node, error = _require_node_auth()
    if error:
        return error
    assert node_id is not None

    task = get_task(tid)
    if not task:
        return jsonify({"error": "Unknown task id"}), 404

    if task.get("assigned_node") != node_id:
        return jsonify({"error": "Task is assigned to another node"}), 403

    claim_token = (request.headers.get("X-Task-Claim") or "").strip()
    if not compare_token(task.get("claim_token"), claim_token):
        return jsonify({"error": "Invalid claim token"}), 403

    if request.mimetype == "application/octet-stream":
        result_bytes = request.get_data()
        # Note: result_bytes can be b"" (empty bytes) which is valid for placeholder tasks.

        # --- Execution receipt verification ---
        receipt_header = (request.headers.get("X-Execution-Receipt") or "").strip()
        if not receipt_header:
            return jsonify({"error": "Missing X-Execution-Receipt header"}), 400

        try:
            receipt = json.loads(base64.b64decode(receipt_header))
        except Exception:
            return jsonify({"error": "Could not decode execution receipt"}), 400

        verified, reason = zkp.verify_receipt(receipt, result_bytes, node_id)
        if not verified:
            bad_count = zkp.increment_bad_receipt_count(node_id)
            if bad_count >= zkp.BAD_RECEIPT_THRESHOLD:
                redis_client.srem("nodes", node_id)
                logger.warning(
                    "Node %s deregistered after %d bad receipts", node_id, bad_count
                )
            # Re-queue the task instead of completing it
            requeue_task(tid, None)
            return jsonify({"error": f"Receipt verification failed: {reason}"}), 403

        # Record instruction usage against the API key's quota
        instruction_count = receipt.get("instruction_count", 0)
        task_job = task.get("job_id", "")
        task_pid = task.get("pid", "")
        if task_pid:
            dep = Deployment.query.filter_by(pid=task_pid).first()
            if dep and dep.api_key:
                quota.record_usage(dep.api_key, instruction_count)

        summary = complete_task(tid, node_id, result_bytes=result_bytes)
        return jsonify(
            {
                "status": "completed",
                "job_status": summary["status"],
                "counts": summary["counts"],
            }
        ), 200

    data = request.get_json(silent=True) or {}
    error_message = (data.get("error") or "Task execution failed").strip()
    summary = fail_task(tid, node_id, error_message=error_message)

    return jsonify(
        {
            "status": "failed",
            "job_status": summary["status"],
            "counts": summary["counts"],
        }
    ), 200
