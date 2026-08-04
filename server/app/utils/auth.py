from __future__ import annotations

from app.extensions import db
from app.models import Deployment, UserAPI
from flask import jsonify, request
from sqlalchemy import select


def extract_api_key() -> str:
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if api_key:
        return api_key

    auth_header = (request.headers.get("Authorization") or "").strip()
    if auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1].strip()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        api_key = (data.get("api_key") or "").strip()
        if api_key:
            return api_key

    return (request.form.get("api_key") or "").strip()


def get_api_client(api_key: str) -> UserAPI | None:
    if not api_key:
        return None

    statement = select(UserAPI).where(UserAPI.api_key == api_key)
    return db.session.scalars(statement).first()


def require_user_api_key():
    api_key = extract_api_key()
    if not api_key:
        return None, (jsonify({"error": "Missing API key"}), 401)

    api_client = get_api_client(api_key)
    if api_client is None:
        return None, (jsonify({"error": "Invalid API key"}), 403)

    return api_client, None


def ensure_deployment_access(api_client: UserAPI, deployment: Deployment):
    if deployment.user_id is None:
        return (
            jsonify(
                {
                    "error": "Deployment is not associated with an owner. Redeploy it with an authenticated /deploy request."
                }
            ),
            409,
        )

    if deployment.user_id != api_client.user_id:
        return jsonify(
            {"error": "API key does not have access to this deployment"}
        ), 403

    return None
