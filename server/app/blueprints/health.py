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

from flask import Blueprint, jsonify
from sqlalchemy import text

from app.extensions import db, redis_client

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
