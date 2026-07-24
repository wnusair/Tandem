"""Web hosting: deploy a web app to nodes and load-balance traffic to it.

The developer's CLI uploads an app bundle plus a start command. We store it,
assign it to some healthy nodes, and each node pulls the assignment and runs the
app in its sandbox. Public traffic to `/app/<pid>/...` is dropped on a queue, a
serving node proxies it to the app, and the response comes back here.
"""

import base64
import pathlib
import secrets
import shlex

from flask import Blueprint, Response, jsonify, request, send_file

from app.blueprints.nodes import _require_node_auth
from app.extensions import db
from app.models import Deployment
from app.utils import serve_queue
from app.utils.auth import ensure_deployment_access, require_user_api_key
from app.utils.task_queue import storage_root

serve_bp = Blueprint("serve", __name__)

# How long the load balancer waits for a node to answer before giving up.
_RESPONSE_TIMEOUT_SECONDS = 30
# How long a node long-polls for the next request before checking back in.
_POLL_TIMEOUT_SECONDS = 5


def _bundle_path(pid: str) -> pathlib.Path:
    return pathlib.Path(storage_root()) / "serve" / pid / "bundle.tar"


@serve_bp.route("/serve/deploy", methods=["POST"])
def serve_deploy():
    """CLI entry point: upload an app bundle and start hosting it."""
    api_client, error = require_user_api_key()
    if error:
        return error
    assert api_client is not None

    if "bundle" not in request.files:
        return jsonify({"error": "Missing app bundle"}), 400

    start_command = (request.form.get("start_command") or "").strip()
    if not start_command:
        return jsonify({"error": "Missing start_command"}), 400

    try:
        replicas = max(1, int(request.form.get("replicas", "1")))
    except ValueError:
        return jsonify({"error": "replicas must be an integer"}), 400

    name = (request.form.get("name") or "app").strip()
    pid = "serve_" + secrets.token_hex(8)

    bundle_path = _bundle_path(pid)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    request.files["bundle"].save(str(bundle_path))

    db.session.add(
        Deployment(
            name=name,
            pid=pid,
            user_id=api_client.user_id,
            api_key=api_client.api_key,
        )
    )
    db.session.commit()

    serve_queue.create_serve_deployment(
        pid,
        start_command=shlex.split(start_command),
        replicas=replicas,
    )
    return jsonify({"pid": pid, "url": f"/app/{pid}/"}), 201


@serve_bp.route("/nodes/serve/claim", methods=["POST"])
def nodes_claim():
    """A node pulls its next serve assignment, if any."""
    node_id, _node, error = _require_node_auth()
    if error:
        return error

    assignment = serve_queue.claim_assignment(node_id)
    if assignment is None:
        return ("", 204)
    return jsonify(assignment)


@serve_bp.route("/nodes/serve/<pid>/bundle", methods=["GET"])
def nodes_bundle(pid):
    """A node downloads the app bundle it was assigned."""
    _node_id, _node, error = _require_node_auth()
    if error:
        return error

    bundle_path = _bundle_path(pid)
    if not bundle_path.exists():
        return jsonify({"error": "Bundle not found"}), 404
    return send_file(str(bundle_path), mimetype="application/x-tar")


@serve_bp.route("/nodes/serve/next", methods=["POST"])
def nodes_next():
    """A serving node long-polls for the next request across its deployments.

    The same call doubles as the node's "still serving" heartbeat for each pid.
    We only heartbeat/serve deployments that are still active; any the node lists
    that we've since removed or marked failed come back in `stop` so the node
    shuts its copy down.
    """
    node_id, _node, error = _require_node_auth()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    pids = [str(p) for p in (data.get("pids") or [])]

    active: list[str] = []
    stop: list[str] = []
    for pid in pids:
        if serve_queue.deployment_active(pid):
            active.append(pid)
            serve_queue.mark_serving(pid, node_id)
        else:
            stop.append(pid)

    claimed = serve_queue.claim_request(active, timeout=_POLL_TIMEOUT_SECONDS)
    request_obj = None
    if claimed is not None:
        req_id, req = claimed
        request_obj = {"req_id": req_id, **req}

    # Nothing to do and nothing to stop -> plain 204 (keeps older nodes happy).
    if request_obj is None and not stop:
        return ("", 204)
    return jsonify({"request": request_obj, "stop": stop})


@serve_bp.route("/nodes/serve/<pid>/failed", methods=["POST"])
def nodes_failed(pid):
    """A node reports it couldn't start this deployment. Mark it failed so it
    stops being re-assigned (otherwise a broken app churns forever)."""
    _node_id, _node, error = _require_node_auth()
    if error:
        return error
    serve_queue.mark_failed(pid)
    return jsonify({"ok": True})


@serve_bp.route("/serve", methods=["GET"])
def serve_list():
    """List the caller's serve deployments and where they're running."""
    api_client, error = require_user_api_key()
    if error:
        return error
    assert api_client is not None

    deployments = []
    for pid in serve_queue.all_deployment_ids():
        row = Deployment.query.filter_by(pid=pid).first()
        if not row or row.user_id != api_client.user_id:
            continue
        meta = serve_queue.get_serve_deployment(pid) or {}
        deployments.append(
            {
                "pid": pid,
                "name": row.name,
                "status": meta.get("status", "running"),
                "replicas": meta.get("replicas"),
                "serving_nodes": serve_queue.healthy_serving_node_ids(pid),
                "url": f"/app/{pid}/",
            }
        )
    return jsonify({"deployments": deployments})


@serve_bp.route("/serve/<pid>", methods=["DELETE"])
def serve_remove(pid):
    """Stop hosting a deployment and forget it. Idempotent."""
    api_client, error = require_user_api_key()
    if error:
        return error
    assert api_client is not None

    row = Deployment.query.filter_by(pid=pid).first()
    if row is not None:
        access_error = ensure_deployment_access(api_client, row)
        if access_error:
            return access_error

    serve_queue.remove_deployment(pid)

    bundle_path = _bundle_path(pid)
    try:
        if bundle_path.exists():
            bundle_path.unlink()
    except OSError:
        pass

    if row is not None:
        db.session.delete(row)
        db.session.commit()

    return jsonify({"ok": True, "pid": pid})


@serve_bp.route("/nodes/serve/response/<req_id>", methods=["POST"])
def nodes_response(req_id):
    """A node hands back the app's response for one request."""
    _node_id, _node, error = _require_node_auth()
    if error:
        return error

    data = request.get_json(silent=True) or {}
    serve_queue.submit_response(
        req_id,
        status=int(data.get("status", 502)),
        headers=data.get("headers", []),
        body_b64=data.get("body_b64", ""),
    )
    return jsonify({"ok": True})


# The public load-balancer route. Anyone can hit a hosted app, so no auth here.
_LB_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]


@serve_bp.route("/app/<pid>/", defaults={"subpath": ""}, methods=_LB_METHODS)
@serve_bp.route("/app/<pid>/<path:subpath>", methods=_LB_METHODS)
def load_balance(pid, subpath):
    """Forward one request to a node hosting `pid` and return its response."""
    if not serve_queue.healthy_serving_node_ids(pid):
        return jsonify({"error": "No healthy node is serving this app yet"}), 503

    headers = [(name, value) for name, value in request.headers.items()]
    body_b64 = base64.b64encode(request.get_data()).decode("ascii")

    path = "/" + subpath
    if request.query_string:
        path += "?" + request.query_string.decode("latin-1")

    req_id = serve_queue.enqueue_request(
        pid,
        method=request.method,
        path=path,
        headers=headers,
        body_b64=body_b64,
    )

    response = serve_queue.wait_for_response(req_id, timeout=_RESPONSE_TIMEOUT_SECONDS)
    if response is None:
        return jsonify({"error": "The app did not respond in time"}), 504

    body = base64.b64decode(response.get("body_b64", ""))
    flask_response = Response(body, status=int(response.get("status", 502)))
    for name, value in response.get("headers", []):
        # Let Flask compute these; forwarding them causes double-encoding issues.
        if name.lower() in ("content-length", "transfer-encoding", "connection"):
            continue
        flask_response.headers[name] = value
    return flask_response
