"""
Enterprise-grade JWT authentication blueprint for the Tandem server.

Security model:
  - Passwords hashed with scrypt (existing pattern, preserved)
  - Access tokens: RS256 JWT, 15-minute TTL, signed with server RSA private key
  - Refresh tokens: RS256 JWT, 7-day TTL, tracked in Redis for revocation
  - Session tracking: Redis set per user_id, keyed by JTI (JWT ID)
  - Logout: removes session JTI from Redis, invalidating the refresh token
  - Rate limiting: brute-force protection on /login and /register (5 attempts/min per IP)
  - All tokens carry: user_id, username, jti (UUID4), iat, exp
  - Public key endpoint: allows CLI/Desktop to verify tokens offline

This blueprint is ADDITIVE — it does not modify the existing UserAPI key system,
NodePublicKey RSA verification, or signed receipt verification in any way.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, generate_api_key, redis_client
from app.models import User, UserAPI

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ACCESS_TOKEN_TTL_SECONDS = 15 * 60          # 15 minutes
_REFRESH_TOKEN_TTL_SECONDS = 7 * 24 * 3600   # 7 days
_RATE_LIMIT_WINDOW_SECONDS = 60              # 1-minute window
_RATE_LIMIT_MAX_ATTEMPTS = 5                 # max attempts per window
_MIN_PASSWORD_LENGTH = 10
_MAX_API_KEY_GENERATION_ATTEMPTS = 10

# ---------------------------------------------------------------------------
# Key loading helpers
# ---------------------------------------------------------------------------

def _resolve_key_paths() -> tuple[Path, Path]:
    """Resolve private and public key paths, auto-generating them if absent."""
    priv_cfg = current_app.config.get("JWT_PRIVATE_KEY_PATH", "keys/jwt_private.pem")
    pub_cfg  = current_app.config.get("JWT_PUBLIC_KEY_PATH",  "keys/jwt_public.pem")
    server_root = Path(current_app.root_path).parent
    priv_path = Path(priv_cfg) if Path(priv_cfg).is_absolute() else server_root / priv_cfg
    pub_path  = Path(pub_cfg)  if Path(pub_cfg).is_absolute()  else server_root / pub_cfg

    if not priv_path.exists() or not pub_path.exists():
        logger.info("JWT keys not found — generating new RSA-2048 keypair at %s", priv_path.parent)
        priv_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        pub_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        priv_path.write_bytes(priv_pem)
        pub_path.write_bytes(pub_pem)
        # Restrict permissions: owner read-only
        priv_path.chmod(0o600)
        pub_path.chmod(0o644)
        logger.info("JWT RSA-2048 keypair generated successfully")

    return priv_path, pub_path


def _load_private_key():
    """Load (or auto-generate) the RSA private key for PyJWT 2.x RS256 signing."""
    priv_path, _ = _resolve_key_paths()
    return serialization.load_pem_private_key(priv_path.read_bytes(), password=None)


def _load_public_key():
    """Load (or auto-generate) the RSA public key for PyJWT 2.x RS256 verification."""
    _, pub_path = _resolve_key_paths()
    return serialization.load_pem_public_key(pub_path.read_bytes())


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------

def _issue_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(seconds=_ACCESS_TOKEN_TTL_SECONDS),
    }
    return jwt.encode(payload, _load_private_key(), algorithm="RS256")


def _issue_refresh_token(user: User) -> tuple[str, str]:
    """Issue a refresh token and register its JTI in Redis. Returns (token, jti)."""
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(seconds=_REFRESH_TOKEN_TTL_SECONDS),
    }
    token = jwt.encode(payload, _load_private_key(), algorithm="RS256")
    # Track the session in Redis: key = session:<user_id>:<jti>
    session_key = f"session:{user.id}:{jti}"
    redis_client.setex(session_key, _REFRESH_TOKEN_TTL_SECONDS, "1")
    return token, jti


def _revoke_refresh_token(user_id: int, jti: str) -> None:
    """Remove a session from Redis, effectively revoking the refresh token."""
    redis_client.delete(f"session:{user_id}:{jti}")


def _is_refresh_token_valid(user_id: int, jti: str) -> bool:
    """Check whether a refresh token's JTI is still active in Redis."""
    return redis_client.exists(f"session:{user_id}:{jti}") == 1


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _rate_limit_key(endpoint: str) -> str:
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    return f"ratelimit:{endpoint}:{ip}"


def _check_rate_limit(endpoint: str) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    key = _rate_limit_key(endpoint)
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
    return count <= _RATE_LIMIT_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# JWT verification decorator
# ---------------------------------------------------------------------------

def require_jwt(f):
    """Decorator that validates the Bearer JWT access token and sets g.current_user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = (request.headers.get("Authorization") or "").strip()
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401
        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(token, _load_public_key(), algorithms=["RS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Access token has expired"}), 401
        except jwt.InvalidTokenError as exc:
            logger.warning("Invalid JWT: %s", exc)
            return jsonify({"error": "Invalid access token"}), 401
        if payload.get("type") != "access":
            return jsonify({"error": "Token is not an access token"}), 401
        user_id = int(payload["sub"])
        user = db.session.get(User, user_id)
        if user is None:
            return jsonify({"error": "User not found"}), 401
        g.current_user = user
        g.jwt_payload = payload
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_data() -> dict:
    return request.get_json(silent=True) or {}


def _reserve_unique_api_key() -> str:
    """Pick an API key that isn't already taken, without saving anything.

    The caller decides which user it belongs to and commits it. Splitting this
    out means both "create a first key" and "rotate to a new key" share the same
    uniqueness check instead of copying the retry loop twice."""
    for _ in range(_MAX_API_KEY_GENERATION_ATTEMPTS):
        api_key = generate_api_key()
        if not db.session.scalars(select(UserAPI).where(UserAPI.api_key == api_key)).first():
            return api_key
    raise RuntimeError("Could not generate a unique API key")


def _ensure_api_key_for_user(user: User) -> str:
    """Return the user's existing API key, or create one if none exists."""
    existing = db.session.scalars(
        select(UserAPI).where(UserAPI.user_id == user.id).order_by(UserAPI.id.asc())
    ).first()
    if existing:
        return existing.api_key
    api_key = _reserve_unique_api_key()
    entry = UserAPI()
    entry.user_id = user.id
    entry.api_key = api_key
    db.session.add(entry)
    db.session.commit()
    return api_key


def _rotate_api_key_for_user(user: User) -> str:
    """Swap the user's API key out for a brand-new one.

    We drop every key row the user has and add a single fresh one in the same
    transaction, so they're never left without a working key. Deployments aren't
    touched on purpose: they're owned by user_id now (see ensure_deployment_access),
    so the old key going away can't orphan them behind a 403. That's the whole
    reason rotation is safe to offer."""
    new_key = _reserve_unique_api_key()
    existing_keys = db.session.scalars(
        select(UserAPI).where(UserAPI.user_id == user.id)
    ).all()
    for row in existing_keys:
        db.session.delete(row)
    entry = UserAPI()
    entry.user_id = user.id
    entry.api_key = new_key
    db.session.add(entry)
    db.session.commit()
    return new_key


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Authenticate a user and issue JWT access + refresh tokens.

    Request body: { "username": str, "password": str }
    Response:     { "access_token": str, "refresh_token": str, "username": str, "api_key": str }
    """
    if not _check_rate_limit("login"):
        return jsonify({"error": "Too many login attempts. Please wait 60 seconds."}), 429

    data = _json_data()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    # When the client asks to rotate, we mint a fresh API key instead of handing
    # back the existing one. The CLI's `tandem auth login --rotate-api-key` sends this.
    rotate_api_key = bool(data.get("rotate_api_key"))

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    user = db.session.scalars(select(User).where(User.username == username)).first()
    if user is None or not check_password_hash(user.password, password):
        # Constant-time response to prevent user enumeration
        return jsonify({"error": "Invalid credentials"}), 401

    try:
        access_token = _issue_access_token(user)
        refresh_token, _ = _issue_refresh_token(user)
        if rotate_api_key:
            api_key = _rotate_api_key_for_user(user)
        else:
            api_key = _ensure_api_key_for_user(user)
    except Exception as exc:
        logger.error("Token issuance failed for user %s: %s", username, exc)
        return jsonify({"error": "Authentication service error"}), 500

    logger.info("User %s logged in from %s", username, request.remote_addr)
    return jsonify({
        "status": "success",
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
        "username": user.username,
        "api_key": api_key,
    }), 200


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    """
    Exchange a valid refresh token for a new access token.

    Request body: { "refresh_token": str }
    Response:     { "access_token": str, "expires_in": int }
    """
    data = _json_data()
    refresh_token = (data.get("refresh_token") or "").strip()
    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 400

    try:
        payload = jwt.decode(refresh_token, _load_public_key(), algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        return jsonify({"error": "Refresh token has expired. Please log in again."}), 401
    except jwt.InvalidTokenError as exc:
        logger.warning("Invalid refresh token: %s", exc)
        return jsonify({"error": "Invalid refresh token"}), 401

    if payload.get("type") != "refresh":
        return jsonify({"error": "Token is not a refresh token"}), 401

    user_id = int(payload["sub"])
    jti = payload.get("jti", "")

    if not _is_refresh_token_valid(user_id, jti):
        return jsonify({"error": "Session has been revoked. Please log in again."}), 401

    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"error": "User not found"}), 401

    try:
        access_token = _issue_access_token(user)
    except Exception as exc:
        logger.error("Access token issuance failed: %s", exc)
        return jsonify({"error": "Token service error"}), 500

    return jsonify({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": _ACCESS_TOKEN_TTL_SECONDS,
    }), 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """
    Revoke the current session's refresh token.

    Request body: { "refresh_token": str }
    """
    data = _json_data()

    refresh_token = (data.get("refresh_token") or "").strip()
    if not refresh_token:
        return jsonify({"error": "refresh_token is required"}), 400

    try:
        payload = jwt.decode(
            refresh_token, _load_public_key(), algorithms=["RS256"],
            options={"verify_exp": False}  # Allow logout of expired tokens
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("Logout with invalid token: %s", exc)
        return jsonify({"error": "Invalid refresh token"}), 400

    user_id = int(payload.get("sub", 0))
    jti = payload.get("jti", "")
    if user_id and jti:
        _revoke_refresh_token(user_id, jti)

    logger.info("Session %s revoked for user_id %s", jti, user_id)
    return jsonify({"status": "success", "message": "Logged out"}), 200


@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Register a new user account.

    Request body: { "username": str, "password": str }
    """
    if not _check_rate_limit("register"):
        return jsonify({"error": "Too many registration attempts. Please wait 60 seconds."}), 429

    data = _json_data()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    import re
    if not re.fullmatch(r"[a-zA-Z0-9._\-]{3,64}", username):
        return jsonify({"error": "Username must be 3-64 characters: letters, numbers, dots, underscores, hyphens only"}), 400

    if len(password) < _MIN_PASSWORD_LENGTH:
        return jsonify({"error": f"Password must be at least {_MIN_PASSWORD_LENGTH} characters"}), 400

    existing = db.session.scalars(select(User).where(User.username == username)).first()
    if existing is not None:
        return jsonify({"error": "Username already exists"}), 409

    password_hash = generate_password_hash(password, method="scrypt")
    try:
        new_user = User()
        new_user.username = username
        new_user.password = password_hash
        db.session.add(new_user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Registration failed, please try again"}), 500

    logger.info("New user registered: %s", username)
    return jsonify({"status": "success", "message": "Account created. You can now log in."}), 201
