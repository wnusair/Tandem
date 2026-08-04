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
from app.utils import task_queue  # noqa: E402


class QueueDepthAndTaskMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls) -> None:
        db.session.remove()
        cls.ctx.pop()

    def setUp(self) -> None:
        redis_client.flushdb()
        db.drop_all()
        db.create_all()

    def test_queue_depth_counts_unassigned_and_per_node_queues(self) -> None:
        job = task_queue.create_job("pid1", "job-name", {}, 2)
        job_id = job["job_id"]

        task_queue.create_task(
            job_id=job_id, pid="pid1", name="n", filename="a.py",
            payload=b"x", assigned_node=None,
        )
        self.assertEqual(task_queue.get_queue_depth(), 1)

        redis_client.sadd("nodes", "node-a")
        task_queue.create_task(
            job_id=job_id, pid="pid1", name="n", filename="b.py",
            payload=b"x", assigned_node="node-a",
        )
        self.assertEqual(task_queue.get_queue_depth(), 2)

    def test_completing_a_task_pops_the_queue_and_records_metrics(self) -> None:
        job = task_queue.create_job("pid1", "job-name", {}, 1)
        job_id = job["job_id"]

        redis_client.sadd("nodes", "node-a")
        redis_client.hset("node:node-a", mapping={"last_seen": task_queue.now_ts()})
        tid = task_queue.create_task(
            job_id=job_id, pid="pid1", name="n", filename="a.py",
            payload=b"x", assigned_node="node-a",
        )
        task_queue.claim_task_for_node("node-a")
        self.assertEqual(task_queue.get_queue_depth(), 0)

        task_queue.complete_task(tid, "node-a", result_bytes=b"result")

        metrics = task_queue.get_task_metrics()
        self.assertEqual(metrics["completed_total"], 1)
        self.assertEqual(metrics["failed_total"], 0)
        self.assertEqual(metrics["terminal_count"], 1)
        self.assertGreaterEqual(metrics["latency_seconds_sum"], 0.0)

    def test_failing_a_task_records_failure_metrics(self) -> None:
        job = task_queue.create_job("pid1", "job-name", {}, 1)
        job_id = job["job_id"]

        redis_client.sadd("nodes", "node-a")
        redis_client.hset("node:node-a", mapping={"last_seen": task_queue.now_ts()})
        tid = task_queue.create_task(
            job_id=job_id, pid="pid1", name="n", filename="a.py",
            payload=b"x", assigned_node="node-a",
        )
        task_queue.claim_task_for_node("node-a")

        task_queue.fail_task(tid, "node-a", error_message="boom")

        metrics = task_queue.get_task_metrics()
        self.assertEqual(metrics["completed_total"], 0)
        self.assertEqual(metrics["failed_total"], 1)
        self.assertEqual(metrics["terminal_count"], 1)


if __name__ == "__main__":
    unittest.main()
