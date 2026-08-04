"""Redis-backed coordination for serve (web-hosting) deployments.

A serve deployment is a long-lived web app. It gets assigned to a few healthy
nodes; each node pulls its assignment and starts the app inside its sandbox.
Incoming HTTP requests land on a per-deployment queue, a serving node picks one
up, proxies it to the app, and pushes the response back for the load balancer to
return to the caller. It mirrors the compute task queue's pull-based shape so a
node still only ever talks *out* to the server.
"""

from __future__ import annotations

import json
import time
import uuid

from redis.exceptions import TimeoutError as RedisTimeoutError

from app.extensions import redis_client
from app.utils.task_queue import (
    decode_value,
    get_healthy_node_ids,
    get_node,
    is_node_healthy,
)

# A serving node must re-report at least this often or we treat it as gone.
SERVE_NODE_STALE_SECONDS = 10.0
# Requests and responses don't linger in Redis forever.
SERVE_REQUEST_TTL_SECONDS = 120

_ALL_DEPLOYMENTS = "serve:deployments"


def _deploy_key(pid: str) -> str:
    return f"serve:deploy:{pid}"


def _node_serve_queue(node_id: str) -> str:
    return f"node:{node_id}:serve"


def _serving_nodes_key(pid: str) -> str:
    return f"serve:{pid}:nodes"


def _pending_key(pid: str) -> str:
    return f"serve:{pid}:pending"


def _request_key(req_id: str) -> str:
    return f"serve:req:{req_id}"


def _response_key(req_id: str) -> str:
    return f"serve:resp:{req_id}"


def create_serve_deployment(pid: str, *, start_command: list[str], replicas: int) -> None:
    redis_client.hset(
        _deploy_key(pid),
        mapping={
            "start_command": json.dumps(start_command),
            "replicas": str(replicas),
            "status": "running",
            "created_at": str(time.time()),
        },
    )
    redis_client.sadd(_ALL_DEPLOYMENTS, pid)
    assign_nodes(pid)


def get_serve_deployment(pid: str) -> dict | None:
    raw = redis_client.hgetall(_deploy_key(pid))
    if not raw:
        return None
    return {decode_value(k): decode_value(v) for k, v in raw.items()}


def all_deployment_ids() -> list[str]:
    return [decode_value(m) for m in redis_client.smembers(_ALL_DEPLOYMENTS)]


def deployment_active(pid: str) -> bool:
    """A deployment the nodes should still be running: it exists and hasn't been
    marked failed. Removed deployments (deleted hash) are inactive too."""
    meta = get_serve_deployment(pid)
    return bool(meta) and meta.get("status", "running") == "running"


def mark_failed(pid: str) -> None:
    """A node couldn't start this app after several tries. Stop handing it out so
    it doesn't churn the assignment queue forever."""
    if redis_client.exists(_deploy_key(pid)):
        redis_client.hset(_deploy_key(pid), "status", "failed")


def remove_deployment(pid: str) -> None:
    """Forget a deployment entirely (user asked to stop it). Nodes serving it
    learn it's gone via `/nodes/serve/next` and shut their copy down."""
    redis_client.srem(_ALL_DEPLOYMENTS, pid)
    redis_client.delete(_deploy_key(pid))
    redis_client.delete(_serving_nodes_key(pid))
    redis_client.delete(_pending_key(pid))


def current_serving_node_ids(pid: str) -> set[str]:
    return {decode_value(m) for m in redis_client.zrange(_serving_nodes_key(pid), 0, -1)}


def assign_nodes(pid: str) -> None:
    """Assign the deployment to up to `replicas` healthy nodes that aren't
    already serving it, by pushing the pid onto each node's serve queue."""
    meta = get_serve_deployment(pid)
    if not meta:
        return
    # Don't re-hand-out a deployment that's been marked failed -- that's what
    # kept a broken app churning the node's serve queue forever.
    if meta.get("status", "running") != "running":
        return
    replicas = int(meta.get("replicas", 1))
    already = current_serving_node_ids(pid)

    for node_id in get_healthy_node_ids():
        if len(already) >= replicas:
            break
        if node_id in already:
            continue
        redis_client.rpush(_node_serve_queue(node_id), pid)
        already.add(node_id)


def claim_assignment(node_id: str) -> dict | None:
    """A node pulls its next serve assignment, if any."""
    raw = redis_client.lpop(_node_serve_queue(node_id))
    if raw is None:
        return None
    pid = decode_value(raw)
    meta = get_serve_deployment(pid)
    if not meta:
        return None
    return {"pid": pid, "start_command": json.loads(meta["start_command"])}


def mark_serving(pid: str, node_id: str) -> None:
    """Record (or refresh) that `node_id` is currently serving `pid`."""
    redis_client.zadd(_serving_nodes_key(pid), {node_id: time.time()})


def healthy_serving_node_ids(pid: str) -> list[str]:
    now = time.time()
    fresh = redis_client.zrangebyscore(
        _serving_nodes_key(pid), now - SERVE_NODE_STALE_SECONDS, "+inf"
    )
    result: list[str] = []
    for raw in fresh:
        node_id = decode_value(raw)
        node = get_node(node_id)
        if node and is_node_healthy(node, now=now):
            result.append(node_id)
    return result


def enqueue_request(pid: str, *, method: str, path: str, headers: list, body_b64: str) -> str:
    req_id = uuid.uuid4().hex
    redis_client.hset(
        _request_key(req_id),
        mapping={
            "pid": pid,
            "method": method,
            "path": path,
            "headers": json.dumps(headers),
            "body_b64": body_b64,
        },
    )
    redis_client.expire(_request_key(req_id), SERVE_REQUEST_TTL_SECONDS)
    redis_client.rpush(_pending_key(pid), req_id)
    redis_client.expire(_pending_key(pid), SERVE_REQUEST_TTL_SECONDS)
    return req_id


def _blpop_or_none(keys: list[str], timeout: float):
    """A blocking pop where a read timeout counts as an empty result.

    On a threaded server, redis-py can let a pooled connection's socket read
    time out right at the edge of the BLPOP block window and raise instead of
    returning nil. For a long-poll that just means "nothing showed up in time",
    so we fold it into None and let the caller poll again -- otherwise every
    idle poll turns into a spurious 500."""
    try:
        return redis_client.blpop(keys, timeout=int(timeout))
    except RedisTimeoutError:
        return None


def claim_request(pids: list[str], timeout: float) -> tuple[str, dict] | None:
    """A serving node long-polls for the next request across the deployments it
    hosts. Returns (req_id, request) or None on timeout."""
    if not pids:
        time.sleep(min(timeout, 1.0))
        return None

    popped = _blpop_or_none([_pending_key(p) for p in pids], timeout=int(timeout))
    if popped is None:
        return None

    _key, raw_req_id = popped
    req_id = decode_value(raw_req_id)
    raw = redis_client.hgetall(_request_key(req_id))
    if not raw:
        return None
    request = {decode_value(k): decode_value(v) for k, v in raw.items()}
    return req_id, request


def submit_response(req_id: str, *, status: int, headers: list, body_b64: str) -> None:
    payload = json.dumps({"status": status, "headers": headers, "body_b64": body_b64})
    redis_client.rpush(_response_key(req_id), payload)
    redis_client.expire(_response_key(req_id), SERVE_REQUEST_TTL_SECONDS)


def wait_for_response(req_id: str, timeout: float) -> dict | None:
    popped = _blpop_or_none([_response_key(req_id)], timeout=int(timeout))
    if popped is None:
        return None
    _key, raw = popped
    return json.loads(decode_value(raw))


def reap_stale_serve_nodes() -> None:
    """Drop serving nodes that have gone silent and top deployments back up to
    their replica count on healthy nodes. Called by the background sweeper."""
    now = time.time()
    for raw_pid in redis_client.smembers(_ALL_DEPLOYMENTS):
        pid = decode_value(raw_pid)
        key = _serving_nodes_key(pid)

        # Forget nodes that haven't reported within the stale window...
        redis_client.zremrangebyscore(key, "-inf", now - SERVE_NODE_STALE_SECONDS)
        # ...and any whose node has otherwise gone unhealthy.
        for node_id in list(current_serving_node_ids(pid)):
            node = get_node(node_id)
            if not node or not is_node_healthy(node, now=now):
                redis_client.zrem(key, node_id)

        # Refill toward the desired replica count on whatever's healthy now.
        assign_nodes(pid)
