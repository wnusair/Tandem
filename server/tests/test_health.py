import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "test-registration-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from app import create_app  # noqa: E402
from app.extensions import db, redis_client  # noqa: E402
from app.utils import task_queue  # noqa: E402


class HealthEndpointTests(unittest.TestCase):
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

    def test_healthz_requires_no_auth_and_returns_ok(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_readyz_returns_200_when_dependencies_are_reachable(self) -> None:
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["checks"], {"redis": "ok", "postgres": "ok"})

    def test_readyz_returns_503_when_redis_is_down_without_leaking_details(self) -> None:
        with patch.object(redis_client, "ping", side_effect=ConnectionError("boom at redis://user:pw@host")):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["checks"]["redis"], "error")
        self.assertNotIn("redis://", response.get_data(as_text=True))
        self.assertNotIn("boom", response.get_data(as_text=True))

    def test_readyz_returns_503_when_postgres_is_down_without_leaking_details(self) -> None:
        with patch.object(
            db.session, "execute", side_effect=Exception("connection to postgresql://user:pw@host failed")
        ):
            response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        body = response.get_json()
        self.assertEqual(body["checks"]["postgres"], "error")
        self.assertNotIn("postgresql://", response.get_data(as_text=True))

    def test_metrics_exposes_queue_depth_and_task_outcome_counters(self) -> None:
        job = task_queue.create_job("pid1", "job-name", {}, 1)
        redis_client.sadd("nodes", "node-a")
        redis_client.hset("node:node-a", mapping={"last_seen": task_queue.now_ts()})
        tid = task_queue.create_task(
            job_id=job["job_id"], pid="pid1", name="n", filename="a.py",
            payload=b"x", assigned_node="node-a",
        )
        task_queue.claim_task_for_node("node-a")
        task_queue.complete_task(tid, "node-a", result_bytes=b"result")

        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "text/plain; version=0.0.4; charset=utf-8")

        body = response.get_data(as_text=True)
        self.assertIn("tandem_queue_depth_tasks 0", body)
        self.assertIn("tandem_nodes_total 1", body)
        self.assertIn("tandem_nodes_healthy 1", body)
        self.assertIn("tandem_tasks_completed_total 1", body)
        self.assertIn("tandem_tasks_failed_total 0", body)
        self.assertIn("tandem_task_latency_seconds_count 1", body)
        self.assertIn("tandem_task_failure_ratio 0.000000", body)
        self.assertIn("# TYPE tandem_queue_depth_tasks gauge", body)
        self.assertIn("# TYPE tandem_tasks_completed_total counter", body)

    def test_metrics_exposes_failure_path_task_metrics(self) -> None:
        job = task_queue.create_job("pid1", "job-name", {}, 1)
        redis_client.sadd("nodes", "node-a")
        redis_client.hset("node:node-a", mapping={"last_seen": task_queue.now_ts()})
        tid = task_queue.create_task(
            job_id=job["job_id"], pid="pid1", name="n", filename="a.py",
            payload=b"x", assigned_node="node-a",
        )
        task_queue.claim_task_for_node("node-a")
        task_queue.fail_task(tid, "node-a", error_message="boom")

        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "text/plain; version=0.0.4; charset=utf-8")

        body = response.get_data(as_text=True)
        self.assertIn("tandem_tasks_failed_total 1", body)
        self.assertIn("tandem_task_failure_ratio 1.000000", body)
        self.assertIn("tandem_task_latency_seconds_sum", body)

    def test_metrics_requires_no_auth(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
