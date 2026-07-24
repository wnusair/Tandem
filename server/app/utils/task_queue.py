from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import pathlib
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.extensions import db, redis_client
from app.models import NodePublicKey, TaskEncryptionKey
from flask import current_app

logger = logging.getLogger(__name__)

NODE_STALE_SECONDS = 5.0
TASK_LEASE_SECONDS = 30.0


def decode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def decode_mapping(raw: dict[Any, Any] | None) -> dict[str, str]:
    if not raw:
        return {}

    decoded: dict[str, str] = {}
    for key, value in raw.items():
        decoded[str(decode_value(key))] = str(decode_value(value))
    return decoded


def decode_list(values: list[Any] | None) -> list[str]:
    if not values:
        return []
    return [str(decode_value(value)) for value in values]


def now_ts() -> str:
    return str(time.time())


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def billed_seconds(task: dict[str, str]) -> int:
    """Whole seconds the server watched this task occupy a node, floored at 1.

    We charge quota against this instead of anything the node hands us. Both the
    claim stamp and "now" come from our own clock (see claim_task_for_node and
    complete_task), so a node can't shrink or pad the number. If a task somehow
    finished without a claim stamp we fall back to when it was created.
    """
    start = safe_float(task.get("claimed_at") or task.get("created_at"), time.time())
    return max(1, math.ceil(time.time() - start))


def compare_token(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return secrets.compare_digest(expected, provided)


def storage_root() -> pathlib.Path:
    # Always return an ABSOLUTE path. Blob paths derived from here get stored and
    # later handed to Flask's send_file(), which resolves a relative path against
    # app.root_path (…/server/app) rather than the cwd -- so a relative
    # TASK_STORAGE_ROOT (e.g. "runtime" from .env) makes send_file look in
    # the wrong place and 500 with FileNotFoundError. Resolving a relative value
    # against the server dir keeps storage independent of the process cwd.
    server_dir = pathlib.Path(current_app.root_path).resolve().parent
    configured = current_app.config.get("TASK_STORAGE_ROOT")
    if configured:
        root = pathlib.Path(configured).expanduser()
        if not root.is_absolute():
            root = server_dir / root
    else:
        root = server_dir / "runtime"

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _blob_suffix(filename: str | None) -> str:
    suffix = pathlib.Path(filename or "").suffix.strip()
    if not suffix:
        return ".bin"
    if suffix.startswith("."):
        return suffix
    return f".{suffix}"


def _unassigned_queue_key(runtime: str | None) -> str:
    normalized = (runtime or "cloudpickle").strip() or "cloudpickle"
    if normalized == "cloudpickle":
        return "tasks:unassigned"
    return f"tasks:unassigned:{normalized}"


def _flag_enabled(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _node_supports_runtime(node: dict[str, str] | None, runtime: str | None) -> bool:
    normalized = (runtime or "cloudpickle").strip() or "cloudpickle"
    if normalized == "wasm":
        return _flag_enabled((node or {}).get("supports_wasm"))

    supports_cloudpickle = (node or {}).get("supports_cloudpickle")
    if supports_cloudpickle is None:
        return True
    return _flag_enabled(supports_cloudpickle)


def _node_unassigned_runtimes(node: dict[str, str]) -> list[str]:
    runtimes = ["cloudpickle"]
    if _node_supports_runtime(node, "wasm"):
        runtimes.insert(0, "wasm")
    return runtimes


def task_blob_path(job_id: str, tid: str, filename: str | None = None) -> pathlib.Path:
    path = storage_root() / "tasks" / job_id / f"{tid}{_blob_suffix(filename)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def result_blob_path(job_id: str, tid: str) -> pathlib.Path:
    path = storage_root() / "results" / job_id / f"{tid}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_bytes(path: pathlib.Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def read_bytes(path_string: str | None) -> bytes | None:
    if not path_string:
        return None

    path = pathlib.Path(path_string)
    if not path.exists():
        return None

    return path.read_bytes()


def remove_file(path_string: str | None) -> None:
    if not path_string:
        return

    path = pathlib.Path(path_string)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def generate_job_id() -> str:
    return f"job_{secrets.token_hex(12)}"


def generate_tid() -> str:
    return f"tid_{secrets.token_hex(12)}"


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def get_job(job_id: str) -> dict[str, str]:
    job = decode_mapping(redis_client.hgetall(f"job:{job_id}"))
    if job:
        job["job_id"] = job_id
    return job


def get_task(tid: str) -> dict[str, str]:
    task = decode_mapping(redis_client.hgetall(f"task:{tid}"))
    if task:
        task["tid"] = tid
    return task


def get_job_task_ids(job_id: str) -> list[str]:
    return decode_list(redis_client.lrange(f"job:{job_id}:tasks", 0, -1))


def get_all_node_ids() -> list[str]:
    return sorted(decode_list(list(redis_client.smembers("nodes") or [])))


def get_node(node_id: str) -> dict[str, str]:
    node = decode_mapping(redis_client.hgetall(f"node:{node_id}"))
    if node:
        node["node_id"] = node_id
    return node


def is_node_healthy(node: dict[str, str] | None, *, now: float | None = None) -> bool:
    if not node:
        return False

    if now is None:
        now = time.time()

    last_seen = safe_float(node.get("last_seen"))
    if last_seen <= 0:
        return False

    return (now - last_seen) <= NODE_STALE_SECONDS


def get_healthy_node_ids(
    *, exclude: str | None = None, required_runtime: str | None = None
) -> list[str]:
    current = time.time()
    healthy: list[str] = []

    for node_id in get_all_node_ids():
        if exclude and node_id == exclude:
            continue

        node = get_node(node_id)
        if not is_node_healthy(node, now=current):
            continue
        if not _node_supports_runtime(node, required_runtime):
            continue

        healthy.append(node_id)

    return healthy


def create_job(
    pid: str, name: str, metadata: dict[str, Any], total_tasks: int
) -> dict[str, str]:
    job_id = generate_job_id()
    job_token = generate_token()
    timestamp = now_ts()

    redis_client.hset(
        f"job:{job_id}",
        mapping={
            "job_id": job_id,
            "job_token": job_token,
            "pid": pid,
            "name": name or "",
            "status": "queued",
            "total_tasks": str(total_tasks),
            "created_at": timestamp,
            "updated_at": timestamp,
            "metadata_json": json.dumps(metadata or {}, default=str),
        },
    )

    # TTL so job keys don't accumulate forever
    redis_client.expire(f"job:{job_id}", 86400)

    return {
        "job_id": job_id,
        "job_token": job_token,
    }


def select_least_loaded_node(available_nodes: list[str], pending: dict[str, int]) -> str:
    """Pick the node with the fewest queued tasks to hand the next one to.

    We count both the tasks already sitting in a node's Redis queue and the ones
    we've assigned so far in this same planning pass (tracked in `pending`), so a
    burst of tasks in one job spreads out across nodes instead of piling onto
    whoever happened to be shortest when we started. The task's DEK is wrapped
    to every registered node's public key at creation time, so any node can run
    it after a failover while the server (holding no private keys) still can't
    read the payload.
    """

    def load(node_id: str) -> int:
        try:
            queued = redis_client.llen(f"node:{node_id}:queue") or 0
        except Exception:
            queued = 0
        return int(queued) + pending.get(node_id, 0)

    chosen = min(available_nodes, key=load)
    pending[chosen] = pending.get(chosen, 0) + 1
    return chosen


def create_task(
    *,
    job_id: str,
    pid: str,
    name: str,
    filename: str,
    payload: bytes,
    assigned_node: str | None,
    runtime: str = "cloudpickle",
    task_name: str = "",
    timeout_ms: int | None = None,
    shard_index: int | None = None,
    shard_total: int | None = None,
    verify_group: str = "",
    verify_replica: bool = False,
) -> str:
    tid = generate_tid()
    timestamp = now_ts()
    blob_path = task_blob_path(job_id, tid, filename)

    # --- Encrypt task payload at rest with AES-256-GCM ---
    dek = os.urandom(32)
    iv = os.urandom(12)
    ciphertext = AESGCM(dek).encrypt(iv, payload, None)

    # Wrap the DEK for EVERY node that has registered a public key, not just the
    # one we happened to assign this task to. Failover can move a task to any
    # healthy node, and each node can only unwrap a DEK that was encrypted to its
    # own public key -- pinning the key to a single node meant every failover
    # produced a job nobody could decrypt. One wrapped copy per node fixes that.
    wrapped_any = False
    for node_key_row in NodePublicKey.query.all():
        try:
            public_key = serialization.load_pem_public_key(
                node_key_row.rsa_public_key_pem.encode("utf-8")
            )
            encrypted_dek = public_key.encrypt(
                dek,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            db.session.add(
                TaskEncryptionKey(
                    tid=tid,
                    job_id=job_id,
                    encrypted_dek_b64=base64.b64encode(encrypted_dek).decode("ascii"),
                    iv_b64=base64.b64encode(iv).decode("ascii"),
                    target_node_id=node_key_row.node_id,
                )
            )
            wrapped_any = True
        except Exception:
            logger.warning(
                "Failed to RSA-wrap DEK for task %s / node %s – skipping that node",
                tid,
                node_key_row.node_id,
                exc_info=True,
            )

    if wrapped_any:
        db.session.commit()
        # Only the encrypted bytes ever touch disk once we know some node can
        # unwrap them.
        write_bytes(blob_path, ciphertext)
    else:
        # Nobody has a registered public key, so an encrypted blob would be
        # undecryptable by everyone. Store the payload as-is and let the node
        # run it in the clear (the node treats a blob with no DEK header as
        # plaintext).
        logger.warning(
            "No registered node public keys – storing task %s payload unencrypted",
            tid,
        )
        write_bytes(blob_path, payload)

    redis_client.hset(
        f"task:{tid}",
        mapping={
            "tid": tid,
            "job_id": job_id,
            "pid": pid,
            "name": name or "",
            "task_name": task_name or pathlib.Path(filename).stem,
            "filename": filename,
            "runtime": runtime,
            "timeout_ms": "" if timeout_ms is None else str(timeout_ms),
            "shard_index": "" if shard_index is None else str(shard_index),
            "shard_total": "" if shard_total is None else str(shard_total),
            "status": "queued",
            "assigned_node": assigned_node or "",
            "blob_path": str(blob_path),
            "result_path": "",
            "output_hash": "",
            "error": "",
            "claim_token": "",
            "download_token": "",
            "created_at": timestamp,
            "updated_at": timestamp,
            "claimed_at": "",
            "completed_at": "",
            "lease_expires_at": "",
            "verify_group": verify_group,
            "verify_replica": "1" if verify_replica else "",
            "verify_status": "",
        },
    )

    # TTL so task keys don't accumulate forever
    redis_client.expire(f"task:{tid}", 86400)

    if verify_group:
        # Every copy joins the group so we can line their results up later.
        redis_client.sadd(f"verify:{verify_group}:members", tid)
        redis_client.expire(f"verify:{verify_group}:members", 86400)

    if not verify_replica:
        # A replica is a shadow copy we run purely to check somebody else's
        # work, so it stays out of the job's task list -- the client should
        # still see one result per task it asked for, not N.
        redis_client.rpush(f"job:{job_id}:tasks", tid)

    if assigned_node:
        redis_client.rpush(f"node:{assigned_node}:queue", tid)
    else:
        redis_client.rpush(_unassigned_queue_key(runtime), tid)

    return tid


def nodes_holding_task_key(tid: str) -> set[str]:
    """Node ids that hold a wrapped copy of this task's DEK.

    An empty set means the task isn't encrypted (no wrapped keys), so any node
    can run it. Otherwise only these nodes can actually decrypt the payload.
    """
    rows = TaskEncryptionKey.query.filter_by(tid=tid).all()
    return {row.target_node_id for row in rows}


def decryptable_destinations(tid: str, candidates: list[str]) -> list[str]:
    """Narrow a list of candidate nodes to ones that can decrypt this task.

    If the task isn't encrypted we leave the candidates untouched. This keeps
    failover from handing an encrypted task to a node that has no wrapped DEK
    copy and would just fail to decrypt it.
    """
    holders = nodes_holding_task_key(tid)
    if not holders:
        return candidates
    return [node_id for node_id in candidates if node_id in holders]


def group_member_ids(verify_group: str) -> list[str]:
    """Every task id in a verification group: the real task plus its replicas."""
    if not verify_group:
        return []
    members = redis_client.smembers(f"verify:{verify_group}:members") or []
    return sorted(decode_list(list(members)))


def nodes_running_group_siblings(tid: str) -> set[str]:
    """Nodes already holding another copy of this task's verification group."""
    task = get_task(tid)
    siblings: set[str] = set()

    for member_tid in group_member_ids(task.get("verify_group") or ""):
        if member_tid == tid:
            continue

        member = get_task(member_tid)
        assigned = (member.get("assigned_node") or "").strip()
        if assigned:
            siblings.add(assigned)

    return siblings


def exclude_group_siblings(tid: str, candidates: list[str]) -> list[str]:
    """Keep a copy that's failing over away from nodes running a sibling copy.

    The whole point of running a task on N nodes is that N *different* nodes do
    the work. If failover handed one copy to a node that already has another,
    a dishonest node would end up agreeing with itself and the check would pass.
    """
    siblings = nodes_running_group_siblings(tid)
    if not siblings:
        return candidates
    return [node_id for node_id in candidates if node_id not in siblings]


def requeue_task(tid: str, assigned_node: str | None) -> None:
    task = get_task(tid)
    if not task:
        return

    timestamp = now_ts()
    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": "queued",
            "assigned_node": assigned_node or "",
            "claim_token": "",
            "download_token": "",
            "lease_expires_at": "",
            "updated_at": timestamp,
        },
    )

    if assigned_node:
        redis_client.rpush(f"node:{assigned_node}:queue", tid)
    else:
        redis_client.rpush(_unassigned_queue_key(task.get("runtime")), tid)


def requeue_stale_tasks() -> None:
    current = time.time()

    for node_id in get_all_node_ids():
        node = get_node(node_id)
        if not node or is_node_healthy(node, now=current):
            continue

        current_tid = (node.get("current_task") or "").strip()
        if not current_tid:
            continue

        task = get_task(current_tid)
        if not task:
            redis_client.hset(f"node:{node_id}", mapping={"current_task": ""})
            continue

        if task.get("status") not in {"claimed", "running"}:
            redis_client.hset(f"node:{node_id}", mapping={"current_task": ""})
            continue

        task_runtime = task.get("runtime") or "cloudpickle"
        healthy_destinations = get_healthy_node_ids(
            exclude=node_id, required_runtime=task_runtime
        )
        healthy_destinations = decryptable_destinations(current_tid, healthy_destinations)
        healthy_destinations = exclude_group_siblings(current_tid, healthy_destinations)
        destination = healthy_destinations[0] if healthy_destinations else None

        requeue_task(current_tid, destination)
        redis_client.hset(f"node:{node_id}", mapping={"current_task": ""})


def drain_node_queue(node_id: str) -> None:
    """Hand everything still waiting in one node's queue to somebody else."""
    queue_key = f"node:{node_id}:queue"

    while True:
        raw_tid = redis_client.lpop(queue_key)
        if raw_tid is None:
            break

        tid = str(decode_value(raw_tid))
        task = get_task(tid)
        # Only re-home work that's still waiting to run; anything already
        # finished or failed can be left where it is.
        if not task or task.get("status") not in {"queued", "claimed", "running"}:
            continue

        runtime = task.get("runtime") or "cloudpickle"
        destinations = get_healthy_node_ids(exclude=node_id, required_runtime=runtime)
        destinations = decryptable_destinations(tid, destinations)
        destinations = exclude_group_siblings(tid, destinations)
        requeue_task(tid, destinations[0] if destinations else None)


def drain_dead_node_queues() -> None:
    """Move still-unclaimed tasks off the queues of nodes that have gone away.

    Tasks are assigned to a specific node's queue when a job is planned. If that
    node dies before it ever claims them, nothing in the claim path would move
    them -- they'd sit there forever. So we sweep the queues of unhealthy nodes
    and requeue whatever is still waiting to a node that's actually alive.
    """
    current = time.time()

    for node_id in get_all_node_ids():
        node = get_node(node_id)
        if not node or is_node_healthy(node, now=current):
            continue

        drain_node_queue(node_id)


def sweep_stale_work() -> None:
    """One failover pass: reclaim work from nodes that have died.

    First reclaims tasks a dead node had already claimed, then drains any tasks
    still waiting in dead nodes' queues. Meant to be called on a timer by the
    background sweeper so failover happens even when no node is polling for work.
    """
    requeue_stale_tasks()
    drain_dead_node_queues()


def get_available_nodes(*, required_runtime: str | None = None) -> list[str]:
    requeue_stale_tasks()
    return get_healthy_node_ids(required_runtime=required_runtime)


def _pop_queue_tid(
    queue_key: str,
    *,
    node_id: str,
    allow_unassigned: bool,
) -> str | None:
    while True:
        raw_tid = redis_client.lpop(queue_key)
        if raw_tid is None:
            return None

        tid = str(decode_value(raw_tid))
        task = get_task(tid)
        if not task:
            continue

        assigned_node = (task.get("assigned_node") or "").strip()
        if assigned_node and assigned_node != node_id:
            continue
        if not allow_unassigned and not assigned_node:
            continue

        return tid


def claim_task_for_node(node_id: str) -> dict[str, str] | None:
    requeue_stale_tasks()

    node = get_node(node_id)
    if not node:
        return None

    current_tid = (node.get("current_task") or "").strip()
    if current_tid:
        existing_task = get_task(current_tid)
        if existing_task and existing_task.get("status") in {"claimed", "running"}:
            return existing_task
        redis_client.hset(f"node:{node_id}", mapping={"current_task": ""})

    tid = _pop_queue_tid(
        f"node:{node_id}:queue",
        node_id=node_id,
        allow_unassigned=False,
    )

    if tid is None:
        for runtime in _node_unassigned_runtimes(node):
            tid = _pop_queue_tid(
                _unassigned_queue_key(runtime),
                node_id=node_id,
                allow_unassigned=True,
            )
            if tid is not None:
                break

    if tid is None:
        return None

    task = get_task(tid)
    if not task:
        return None
    if not _node_supports_runtime(node, task.get("runtime")):
        requeue_task(tid, None)
        return None

    # Don't hand an encrypted task off the unassigned queue to a node that has
    # no wrapped DEK copy (e.g. one that registered after the task was created)
    # -- it could download the blob but never decrypt it.
    holders = nodes_holding_task_key(tid)
    if holders and node_id not in holders:
        requeue_task(tid, None)
        return None

    # Same idea for verification replicas: one node must never end up running
    # two copies of the same task, or it would just be checking its own work.
    if node_id in nodes_running_group_siblings(tid):
        requeue_task(tid, None)
        return None

    claim_token = generate_token()
    download_token = generate_token()
    current = time.time()
    timestamp = str(current)

    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": "claimed",
            "assigned_node": node_id,
            "claim_token": claim_token,
            "download_token": download_token,
            "claimed_at": timestamp,
            "lease_expires_at": str(current + TASK_LEASE_SECONDS),
            "updated_at": timestamp,
        },
    )

    redis_client.hset(
        f"node:{node_id}",
        mapping={
            "current_task": tid,
            "last_seen": timestamp,
        },
    )

    task = get_task(tid)
    task["claim_token"] = claim_token
    task["download_token"] = download_token
    return task


def extend_task_lease(node_id: str) -> None:
    node = get_node(node_id)
    if not node:
        return

    current_tid = (node.get("current_task") or "").strip()
    if not current_tid:
        return

    task = get_task(current_tid)
    if not task or task.get("status") not in {"claimed", "running"}:
        return

    current = time.time()
    redis_client.hset(
        f"task:{current_tid}",
        mapping={
            "lease_expires_at": str(current + TASK_LEASE_SECONDS),
            "updated_at": str(current),
        },
    )


def delete_task_keys(tid: str) -> None:
    """Drop all wrapped DEK copies for a task once it's reached a terminal state.

    We keep every node's copy around while the task is still runnable so it can
    fail over; only when it's done (or failed for good) do we clear them.
    """
    try:
        TaskEncryptionKey.query.filter_by(tid=tid).delete()
        db.session.commit()
    except Exception:
        logger.warning("Failed to delete encryption keys for task %s", tid, exc_info=True)


def settled_status(task: dict[str, str], terminal_status: str) -> str:
    """Where a task lands once the node running it reports back.

    Usually that's just the terminal status it earned. But the task the client
    actually reads sits in `verifying` while the rest of its group finishes,
    because until we've compared the copies we don't know whether to trust this
    answer -- and that pause is what lets us repair a bad one before anybody
    sees it. Replicas settle normally; nothing is reading them.
    """
    if task.get("verify_group") and not _flag_enabled(task.get("verify_replica")):
        return "verifying"
    return terminal_status


def release_task_payload(tid: str, task: dict[str, str]) -> None:
    """Drop the encrypted payload and its wrapped keys now the task is done.

    A task in a verification group holds on to both until the whole group
    settles, since the comparison may still need them.
    """
    if task.get("verify_group"):
        return

    remove_file(task.get("blob_path"))
    delete_task_keys(tid)


def complete_task(tid: str, node_id: str, *, result_bytes: bytes) -> dict[str, Any]:
    task = get_task(tid)
    if not task:
        raise KeyError(f"Unknown task id: {tid}")

    result_path = result_blob_path(task["job_id"], tid)
    write_bytes(result_path, result_bytes)

    timestamp = now_ts()
    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": settled_status(task, "completed"),
            "result_path": str(result_path),
            # The same digest the execution receipt carries, kept around so a
            # verification group can compare copies without re-reading blobs.
            "output_hash": hashlib.sha256(result_bytes).hexdigest(),
            "error": "",
            "claim_token": "",
            "download_token": "",
            "lease_expires_at": "",
            "completed_at": timestamp,
            "updated_at": timestamp,
        },
    )
    redis_client.hset(
        f"node:{node_id}", mapping={"current_task": "", "last_seen": timestamp}
    )

    release_task_payload(tid, task)
    return refresh_job_status(task["job_id"])


def fail_task(tid: str, node_id: str, *, error_message: str) -> dict[str, Any]:
    task = get_task(tid)
    if not task:
        raise KeyError(f"Unknown task id: {tid}")

    timestamp = now_ts()
    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": settled_status(task, "failed"),
            "error": error_message,
            "claim_token": "",
            "download_token": "",
            "lease_expires_at": "",
            "completed_at": timestamp,
            "updated_at": timestamp,
        },
    )
    redis_client.hset(
        f"node:{node_id}", mapping={"current_task": "", "last_seen": timestamp}
    )

    release_task_payload(tid, task)
    return refresh_job_status(task["job_id"])


def _task_summary(task: dict[str, str], tid: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "tid": tid,
        "task_name": task.get("task_name")
        or pathlib.Path(task.get("filename") or "").stem,
        "filename": task.get("filename") or "",
        "runtime": task.get("runtime") or "cloudpickle",
        "status": task.get("status") or "queued",
        "assigned_node": task.get("assigned_node") or "",
        "error": task.get("error") or None,
        "created_at": task.get("created_at") or "",
        "claimed_at": task.get("claimed_at") or "",
        "completed_at": task.get("completed_at") or "",
    }

    shard_total = safe_int(task.get("shard_total"))
    if shard_total > 1:
        item["shard_index"] = safe_int(task.get("shard_index"))
        item["shard_total"] = shard_total

    # Only tasks we actually ran redundantly carry a verdict, so leave the field
    # off entirely for everything else rather than reporting an empty string.
    verify_status = task.get("verify_status") or ""
    if verify_status:
        item["verify_status"] = verify_status

    return item


def refresh_job_status(job_id: str) -> dict[str, Any]:
    task_ids = get_job_task_ids(job_id)
    counts = {
        "queued": 0,
        "claimed": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
    }
    tasks: list[dict[str, Any]] = []

    for tid in task_ids:
        task = get_task(tid)
        if not task:
            continue

        status = task.get("status") or "queued"
        if status not in counts:
            counts[status] = 0
        counts[status] += 1

        tasks.append(_task_summary(task, tid))

    total_tasks = len(task_ids)
    done = total_tasks > 0 and (counts["completed"] + counts["failed"] == total_tasks)

    if done and counts["failed"]:
        overall_status = "failed"
    elif done:
        overall_status = "completed"
    elif counts["running"] or counts["claimed"] or counts.get("verifying"):
        # A task waiting on its verification group has already run, but the job
        # isn't finished until we know the answer holds up.
        overall_status = "running"
    else:
        overall_status = "queued"

    redis_client.hset(
        f"job:{job_id}",
        mapping={
            "status": overall_status,
            "updated_at": now_ts(),
        },
    )

    return {
        "job_id": job_id,
        "status": overall_status,
        "done": done,
        "total_tasks": total_tasks,
        "counts": counts,
        "tasks": tasks,
    }


def get_job_results(job_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for tid in get_job_task_ids(job_id):
        task = get_task(tid)
        if not task:
            continue

        item = _task_summary(task, tid)

        if item["status"] == "completed":
            result_bytes = read_bytes(task.get("result_path")) or b""
            item["result_b64"] = base64.b64encode(result_bytes).decode("ascii")
        elif task.get("error"):
            item["error"] = task.get("error")

        results.append(item)

    return results
