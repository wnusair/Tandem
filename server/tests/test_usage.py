import os
import tempfile
import unittest

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "test-registration-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from app import create_app  # noqa: E402
from app.extensions import db, redis_client  # noqa: E402
from app.models import User, UserAPI  # noqa: E402
from app.utils import quota, usage  # noqa: E402


class UsageEndpointTests(unittest.TestCase):
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
        redis_client.flushdb()
        db.drop_all()
        db.create_all()

    def _make_user_with_key(self, api_key: str) -> None:
        user = User(username="tester", password="unused")
        db.session.add(user)
        db.session.flush()
        db.session.add(UserAPI(user_id=user.id, api_key=api_key))
        db.session.commit()

    def test_reports_measured_compute_and_placeholders(self) -> None:
        self._make_user_with_key("KEY123")
        quota.record_usage("KEY123", 250)

        response = self.client.get("/api/v1/usage", headers={"X-API-Key": "KEY123"})
        self.assertEqual(response.status_code, 200)

        resources = {r["type"]: r for r in response.get_json()["resources"]}

        self.assertEqual(resources["compute"]["source"], usage.MEASURED)
        self.assertEqual(resources["compute"]["used"], 250)

        for placeholder in ("ram", "storage", "cpu", "gpu"):
            self.assertEqual(resources[placeholder]["source"], usage.PLACEHOLDER)
            self.assertEqual(resources[placeholder]["used"], 0)

        # RAM and storage advertise the 5 GiB per-account ceiling.
        self.assertEqual(resources["ram"]["limit"], usage.ACCOUNT_RAM_LIMIT_BYTES)
        self.assertEqual(resources["storage"]["limit"], usage.ACCOUNT_STORAGE_LIMIT_BYTES)

    def test_cpu_and_ram_become_measured_once_a_node_is_registered(self) -> None:
        self._make_user_with_key("KEY123")

        register_response = self.client.post(
            "/nodes/register",
            json={"supports_wasm": True, "cpu_cores": 8, "memory_mb": 16000},
            headers={"Authorization": "Bearer KEY123"},
        )
        self.assertEqual(register_response.status_code, 201)

        response = self.client.get("/api/v1/usage", headers={"X-API-Key": "KEY123"})
        resources = {r["type"]: r for r in response.get_json()["resources"]}

        self.assertEqual(resources["cpu"]["source"], usage.MEASURED)
        self.assertEqual(resources["cpu"]["limit"], 8)
        self.assertEqual(resources["ram"]["source"], usage.MEASURED)
        self.assertEqual(resources["ram"]["limit"], 16000 * 2**20)

        # storage and gpu aren't wired up to anything yet, still placeholders.
        for placeholder in ("storage", "gpu"):
            self.assertEqual(resources[placeholder]["source"], usage.PLACEHOLDER)

    def test_compute_sum_across_a_users_keys(self) -> None:
        user = User(username="tester", password="unused")
        db.session.add(user)
        db.session.flush()
        db.session.add(UserAPI(user_id=user.id, api_key="KEY_A"))
        db.session.add(UserAPI(user_id=user.id, api_key="KEY_B"))
        db.session.commit()

        quota.record_usage("KEY_A", 100)
        quota.record_usage("KEY_B", 40)

        response = self.client.get("/api/v1/usage", headers={"X-API-Key": "KEY_A"})
        resources = {r["type"]: r for r in response.get_json()["resources"]}
        self.assertEqual(resources["compute"]["used"], 140)

    def test_requires_an_api_key(self) -> None:
        response = self.client.get("/api/v1/usage")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
