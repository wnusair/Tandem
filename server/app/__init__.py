import os
import pathlib
import secrets

from dotenv import load_dotenv
from flask import Flask

from app.extensions import db, redis_client

load_dotenv()


def _resolve_node_registration_token(server_dir: pathlib.Path) -> str:
    """The bearer token a node must send to POST /nodes/register.

    An explicit TANDEM_NODE_REGISTRATION_TOKEN wins. Otherwise we reuse or
    generate a random token on disk, so registration is never open by default."""
    env_token = os.environ.get("TANDEM_NODE_REGISTRATION_TOKEN")
    if env_token:
        return env_token

    token_path = server_dir / "keys" / "node_registration_token.txt"
    if token_path.exists():
        return token_path.read_text(encoding="utf-8").strip()

    token = secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)

    print(f"\nGenerated a node registration token, saved to {token_path}.")
    print("Logged-in users don't need it. For a headless node with no account, run:")
    print(f"  tandem settings set-registration-token {token}\n")

    return token


def create_app():
    app = Flask(__name__)

    # Behind a load balancer or reverse proxy, trust one hop of forwarding
    # headers so request.host_url and the scheme are right when we build the
    # URLs we hand back to nodes.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    server_dir = pathlib.Path(__file__).resolve().parents[1]

    database_uri = os.environ.get("DATABASE_URL")
    if not database_uri:
        database_uri = f"sqlite:///{server_dir / 'dev.db'}"
    elif database_uri.startswith("sqlite:///") and not database_uri.startswith("sqlite:////"):
        # Relative sqlite paths are ambiguous (resolved against the process's cwd,
        # not this file's location), so anchor them to the server/ directory.
        relative_path = database_uri[len("sqlite:///") :]
        if not pathlib.Path(relative_path).is_absolute():
            absolute_path = (server_dir / relative_path).resolve()
            database_uri = f"sqlite:///{absolute_path}"

    lower = database_uri.lower()
    allowed_prefixes = (
        "postgresql://",
        "postgres://",
        "postgresql+",
        "sqlite:///",
        "sqlite:",
    )
    if not any(lower.startswith(p) for p in allowed_prefixes):
        raise RuntimeError(
            'DATABASE_URL must be a Postgres or SQLite URI (start with "postgresql://" or "sqlite:///")'
        )

    app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        app.config["REDIS_URL"] = redis_url

    task_storage_root = os.environ.get("TASK_STORAGE_ROOT")
    if task_storage_root:
        app.config["TASK_STORAGE_ROOT"] = task_storage_root

    # Redundant execution: what share of tasks (0-100) get run on several nodes
    # at once so their results can be compared. Off unless someone turns it on,
    # since it multiplies the cost of every task it touches.
    app.config["VERIFY_SAMPLE_PERCENT"] = os.environ.get(
        "TANDEM_VERIFY_SAMPLE_PERCENT", 0
    )
    app.config["VERIFY_COPIES"] = os.environ.get("TANDEM_VERIFY_COPIES", 3)
    app.config["VERIFY_TIMEOUT_SECONDS"] = os.environ.get(
        "TANDEM_VERIFY_TIMEOUT_SECONDS", 300
    )

    app.config["NODE_REGISTRATION_TOKEN"] = _resolve_node_registration_token(server_dir)

    redis_client.init_app(app)

    # JWT key paths — can be overridden via environment variables
    app.config["JWT_PRIVATE_KEY_PATH"] = os.environ.get(
        "JWT_PRIVATE_KEY_PATH", "keys/jwt_private.pem"
    )
    app.config["JWT_PUBLIC_KEY_PATH"] = os.environ.get(
        "JWT_PUBLIC_KEY_PATH", "keys/jwt_public.pem"
    )

    from app.blueprints.auth import auth_bp
    from app.blueprints.deploy import deploy_bp
    from app.blueprints.desktop import desktop_bp
    from app.blueprints.index import index_bp
    from app.blueprints.nodes import nodes_bp
    from app.blueprints.serve import serve_bp
    from app.blueprints.start import start_bp
    from app.blueprints.usage import usage_bp

    app.register_blueprint(index_bp, url_prefix="/")
    app.register_blueprint(start_bp, url_prefix="/start")
    app.register_blueprint(deploy_bp, url_prefix="/deploy")
    app.register_blueprint(nodes_bp, url_prefix="/nodes")
    app.register_blueprint(usage_bp, url_prefix="/api/v1")
    # Web hosting: /serve/deploy, /nodes/serve/*, and the public /app/<pid>/ LB.
    app.register_blueprint(serve_bp)
    # JWT-based auth for CLI and Desktop app
    app.register_blueprint(auth_bp, url_prefix="/api/v1/auth")
    # Desktop/CLI-specific routes (require JWT)
    app.register_blueprint(desktop_bp, url_prefix="/api/v1/desktop")

    with app.app_context():
        import importlib

        importlib.import_module("app.models")

        db.init_app(app)
        try:
            db.create_all()
        except Exception as e:
            print("Warning: could not create database tables at startup:", e)
            # continue without stopping the app; DB may be unavailable locally

    # Start the background failover sweeper so work gets reclaimed off dead nodes
    # even when nothing is polling. Skipped during tests and if explicitly turned
    # off (e.g. a one-off management process that shouldn't sweep).
    if not app.config.get("TESTING") and os.environ.get("TANDEM_DISABLE_SWEEPER") != "1":
        from app.utils.sweeper import start_sweeper

        start_sweeper(app)

    return app
