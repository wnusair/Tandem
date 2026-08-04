"""

Create a unique PID / slug and save it to an sqlalchemy db.

"""

import secrets

from app.extensions import db
from app.models import Deployment
from app.utils.auth import require_user_api_key
from app.utils.toml_reader import extract_name, parse_toml_string
from flask import Blueprint, jsonify, request

deploy_bp = Blueprint("deploy", __name__)


@deploy_bp.route("/", methods=["POST"])
def deploy():
    """
    Receives name, creates PID, and saves a
    deployment to db.
    """

    api_client, error = require_user_api_key()
    if error:
        return error
    assert api_client is not None

    data = request.get_json(silent=True) or {}

    name = None

    # toml file sent; default
    if "toml_file" in request.files:
        toml_file = request.files["toml_file"]
        parsed = parse_toml_string(toml_file)
        name = extract_name(parsed)

    # Fallback to JSON
    if not name:
        name = data.get("name")

    if not name:
        return jsonify({"error": "Name is required"}), 400

    pid = secrets.token_hex(8)

    try:
        new_deployment = Deployment()
        new_deployment.name = name
        new_deployment.pid = pid
        new_deployment.user_id = api_client.user_id
        new_deployment.api_key = api_client.api_key

        db.session.add(new_deployment)
        db.session.commit()

        return jsonify(
            {"message": "Deployment Successful", "name": name, "pid": pid}
        ), 201
    except Exception as e:
        db.session.rollback()

        return jsonify({"error": "Oopsie with server", "details": str(e)}), 500
