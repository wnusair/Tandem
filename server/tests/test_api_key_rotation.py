import os
import tempfile
import unittest

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "rotation-test-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from app import create_app  # noqa: E402
from app.extensions import db, redis_client  # noqa: E402
from app.models import Deployment  # noqa: E402
from app.utils.auth import ensure_deployment_access, get_api_client  # noqa: E402


class ApiKeyRotationEndpointTests(unittest.TestCase):
    """`POST /login {rotate_api_key: true}` must mint a brand-new key, kill the old
    one, and -- the part that matters -- leave the user's existing deployments
    reachable, because they're owned by user_id now, not by the key. This is the
    server half of the `tandem auth login --rotate-api-key` contract the CLI already
    ships (and asserts in cli/tests/test_auth_command.py)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls) -> None:
        db.session.remove()
        cls.ctx.pop()

    def setUp(self) -> None:
        # flushdb also clears the login rate-limit counters between tests.
        redis_client.flushdb()
        db.drop_all()
        db.create_all()

    def _register_and_login(self, username: str, password: str) -> str:
        register = self.client.post(
            "/api/v1/auth/register", json={"username": username, "password": password}
        )
        self.assertIn(register.status_code, (201, 409))
        login = self.client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(login.status_code, 200, login.get_data(as_text=True))
        return login.get_json()["api_key"]

    def test_rotation_issues_new_key_and_keeps_deployments(self) -> None:
        password = "rotate-me-please"
        key1 = self._register_and_login("rotator", password)

        # A deployment created under the first key, owned by the user.
        owner = get_api_client(key1)
        self.assertIsNotNone(owner)
        db.session.add(
            Deployment(name="app", pid="pid_rot", user_id=owner.user_id, api_key=key1)
        )
        db.session.commit()

        # Log in again asking to rotate -- a different key comes back.
        rotated = self.client.post(
            "/api/v1/auth/login",
            json={"username": "rotator", "password": password, "rotate_api_key": True},
        )
        self.assertEqual(rotated.status_code, 200, rotated.get_data(as_text=True))
        key2 = rotated.get_json()["api_key"]
        self.assertNotEqual(key1, key2)

        # The old key is dead; the new one resolves to the same user.
        self.assertIsNone(get_api_client(key1))
        new_owner = get_api_client(key2)
        self.assertIsNotNone(new_owner)
        self.assertEqual(new_owner.user_id, owner.user_id)

        # The deployment made under the old key is still accessible with the new
        # key. Before the ownership fix this is exactly what broke -- a rotation
        # turned every deployment into a 403.
        deployment = Deployment.query.filter_by(pid="pid_rot").first()
        self.assertIsNone(ensure_deployment_access(new_owner, deployment))

    def test_login_without_rotate_keeps_same_key(self) -> None:
        password = "keep-this-key-please"
        key1 = self._register_and_login("steady", password)
        again = self.client.post(
            "/api/v1/auth/login",
            json={"username": "steady", "password": password},
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.get_json()["api_key"], key1)


if __name__ == "__main__":
    unittest.main()
