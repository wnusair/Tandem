"""
Desktop and CLI API routes for the Tandem server.

All routes here require a valid JWT access token issued by
/api/v1/auth/login. The UserAPI key system and node receipt verification are
separate and unaffected.

Routes:
  GET  /api/v1/desktop/sdks   — List available SDKs
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from app.blueprints.auth import require_jwt

logger = logging.getLogger(__name__)

desktop_bp = Blueprint("desktop", __name__)

# The SDKs the CLI can discover and install. There's one today; add entries here
# as more get published. `download_url` is None because the CLI ships the SDK
# bundled in, so there's no server-side download yet.
_SDK_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "tandem-python-sdk",
        "language": "Python",
        "description": "Official Python SDK — task decorators, RPC dispatch, and WASM building for Tandem jobs",
        "version": "0.1.0",
        "download_url": None,
    },
]


@desktop_bp.route("/sdks", methods=["GET"])
@require_jwt
def list_sdks():
    """
    Return the list of SDKs available for installation.
    Accepts an optional ?q= query parameter for filtering.
    """
    query = (request.args.get("q") or "").strip().lower()
    sdks = _SDK_REGISTRY
    if query:
        sdks = [
            s for s in sdks
            if query in s["name"].lower()
            or query in (s.get("language") or "").lower()
            or query in (s.get("description") or "").lower()
        ]
    return jsonify({"sdks": sdks}), 200
