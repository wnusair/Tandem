"""
Unauthenticated operational endpoints for deployment monitoring.

Routes:
  GET /healthz  — liveness: is the process up and able to handle a request.
  GET /readyz   — readiness: can it also reach Redis and Postgres.
  GET /metrics  — Prometheus text-exposition metrics (added in a later change).

None of these require authentication, and none of their responses include
connection strings, credentials, or task payloads. See docs/observability.md.
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, jsonify
from sqlalchemy import text

from app.extensions import db, redis_client
from app.utils.task_queue import (
    get_all_node_ids,
    get_healthy_node_ids,
    get_queue_depth,
    get_task_metrics,
)

logger = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@health_bp.route("/readyz", methods=["GET"])
def readyz():
    checks = {"redis": "ok", "postgres": "ok"}

    try:
        redis_client.ping()
    except Exception:
        logger.error("readyz: redis check failed", exc_info=True)
        checks["redis"] = "error"

    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        logger.error("readyz: postgres check failed", exc_info=True)
        checks["postgres"] = "error"
        db.session.rollback()

    ready = all(value == "ok" for value in checks.values())
    status_code = 200 if ready else 503
    return (
        jsonify({"status": "ready" if ready else "not_ready", "checks": checks}),
        status_code,
    )


def _render_prometheus_metrics() -> str:
    queue_depth = get_queue_depth()
    nodes_total = len(get_all_node_ids())
    nodes_healthy = len(get_healthy_node_ids())
    task_metrics = get_task_metrics()
    terminal_count = task_metrics["terminal_count"]
    failure_ratio = (
        task_metrics["failed_total"] / terminal_count if terminal_count else 0.0
    )

    lines = [
        "# HELP tandem_queue_depth_tasks Tasks waiting in a Redis queue to be claimed by a node.",
        "# TYPE tandem_queue_depth_tasks gauge",
        f"tandem_queue_depth_tasks {queue_depth}",
        "# HELP tandem_nodes_total Nodes currently tracked by the server.",
        "# TYPE tandem_nodes_total gauge",
        f"tandem_nodes_total {nodes_total}",
        "# HELP tandem_nodes_healthy Nodes with a heartbeat inside the staleness window.",
        "# TYPE tandem_nodes_healthy gauge",
        f"tandem_nodes_healthy {nodes_healthy}",
        "# HELP tandem_tasks_completed_total Tasks that finished successfully, cumulative since the last Redis flush.",
        "# TYPE tandem_tasks_completed_total counter",
        f"tandem_tasks_completed_total {task_metrics['completed_total']}",
        "# HELP tandem_tasks_failed_total Tasks that finished with an error, cumulative since the last Redis flush.",
        "# TYPE tandem_tasks_failed_total counter",
        f"tandem_tasks_failed_total {task_metrics['failed_total']}",
        "# HELP tandem_task_latency_seconds_sum Sum of queue-to-terminal latency across finished tasks, in seconds, cumulative since the last Redis flush.",
        "# TYPE tandem_task_latency_seconds_sum counter",
        f"tandem_task_latency_seconds_sum {task_metrics['latency_seconds_sum']:.6f}",
        "# HELP tandem_task_latency_seconds_count Finished tasks counted in tandem_task_latency_seconds_sum, cumulative since the last Redis flush.",
        "# TYPE tandem_task_latency_seconds_count counter",
        f"tandem_task_latency_seconds_count {terminal_count}",
        "# HELP tandem_task_failure_ratio Fraction of finished tasks that failed, from 0 to 1.",
        "# TYPE tandem_task_failure_ratio gauge",
        f"tandem_task_failure_ratio {failure_ratio:.6f}",
        "",
    ]
    return "\n".join(lines)


@health_bp.route("/metrics", methods=["GET"])
def metrics():
    return Response(
        _render_prometheus_metrics(), content_type="text/plain; version=0.0.4; charset=utf-8"
    )
