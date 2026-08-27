"""One task, one accepted result, one bill -- even when failover gets involved.

A node that went quiet for five seconds used to have its in-flight task handed
straight to somebody else, and nothing noticed when two answers came back for
the same task. Now the sweeper waits for the lease to expire, and the result
endpoint settles each task exactly once.
"""

import base64
import hashlib
import json
import os
import tempfile
import time
import unittest
from collections import namedtuple

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "shared-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db, redis_client  # noqa: E402
from app.models import Deployment, User  # noqa: E402
from app.utils import quota, receipts, task_queue  # noqa: E402


_Node = namedtuple("_Node", "node_id token private_key")


def _make_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_key, public_pem


def _signed_receipt(private_key, tid: str, result_bytes: bytes) -> str:
    """Build the X-Execution-Receipt header the way a real node does."""
    instruction_count = 4242
    output_hash = hashlib.sha256(result_bytes).hexdigest()
    memory_hash = hashlib.sha256(b"").hexdigest()

    signature = private_key.sign(
        receipts.build_receipt_message(
            tid, instruction_count, memory_hash, output_hash
        ),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256(),
    )

    receipt = {
        "tid": tid,
        "instruction_count": instruction_count,
        "memory_hash": memory_hash,
        "output_hash": output_hash,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return base64.b64encode(json.dumps(receipt).encode("utf-8")).decode("ascii")


class LeaseFailoverTests(unittest.TestCase):
    """A node that misses a heartbeat still owns the task it's running."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        # Failover looks up wrapped task keys, so the tables have to exist.
        db.create_all()

    @classmethod
    def tearDownClass(cls) -> None:
        db.session.remove()
        cls.ctx.pop()

    def setUp(self) -> None:
        redis_client.flushdb()

        now = time.time()
        redis_client.sadd("nodes", "quiet", "alive")
        redis_client.hset(
            "node:quiet",
            mapping={
                "node_token": "t",
                "last_seen": str(now - 10),
                "supports_wasm": "1",
                "current_task": "T1",
            },
        )
        redis_client.hset(
            "node:alive",
            mapping={"node_token": "t", "last_seen": str(now), "supports_wasm": "1"},
        )

    def _put_task(self, lease_expires_at: str) -> None:
        redis_client.hset(
            "task:T1",
            mapping={
                "status": "running",
                "runtime": "wasm",
                "assigned_node": "quiet",
                "lease_expires_at": lease_expires_at,
            },
        )

    def _alive_queue(self) -> list[str]:
        return [
            str(task_queue.decode_value(x))
            for x in redis_client.lrange("node:alive:queue", 0, -1)
        ]

    def test_a_live_lease_keeps_the_task_where_it_is(self) -> None:
        self._put_task(str(time.time() + 20))

        task_queue.sweep_stale_work()

        task = task_queue.get_task("T1")
        self.assertEqual(task["assigned_node"], "quiet")
        self.assertEqual(task["status"], "running")
        self.assertEqual(self._alive_queue(), [], "task was stolen mid-flight")
        # The node keeps its pointer, so it can still report a result.
        self.assertEqual(task_queue.get_node("quiet")["current_task"], "T1")

    def test_failover_still_happens_once_the_lease_runs_out(self) -> None:
        self._put_task(str(time.time() - 1))

        task_queue.sweep_stale_work()

        task = task_queue.get_task("T1")
        self.assertEqual(task["assigned_node"], "alive")
        self.assertEqual(task["status"], "queued")
        self.assertIn("T1", self._alive_queue())
        self.assertEqual(task_queue.get_node("quiet").get("current_task", ""), "")

    def test_a_task_with_no_lease_at_all_still_fails_over(self) -> None:
        # Requeuing clears the field, so an empty lease must not read as forever.
        self._put_task("")

        task_queue.sweep_stale_work()

        self.assertEqual(task_queue.get_task("T1")["assigned_node"], "alive")


class ResultIdempotencyTests(unittest.TestCase):
    """Whoever reports first owns the outcome; everything after is a duplicate."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = create_app()
        cls.ctx = cls.app.app_context()
        cls.ctx.push()
        cls.client = cls.app.test_client()
        cls.shared_token = cls.app.config["NODE_REGISTRATION_TOKEN"]

    @classmethod
    def tearDownClass(cls) -> None:
        db.session.remove()
        cls.ctx.pop()

    def setUp(self) -> None:
        redis_client.flushdb()
        db.drop_all()
        db.create_all()

        # The deployment ties the task's pid to the API key work is billed to.
        user = User(username="biller", password="unused")
        db.session.add(user)
        db.session.commit()
        db.session.add(
            Deployment(name="bill", pid="pid_test", user_id=user.id, api_key="BILLKEY")
        )
        db.session.commit()

        # Both register before the task exists so either could legitimately run it.
        self.node_a = self._register_node()
        self.node_b = self._register_node()

        self.job_id = task_queue.create_job(
            pid="pid_test", name="dedup-test", metadata={}, total_tasks=1
        )["job_id"]
        self.tid = task_queue.create_task(
            job_id=self.job_id,
            pid="pid_test",
            name="dedup-test",
            filename="task.wasm",
            payload=b"the work to be done",
            assigned_node=self.node_a.node_id,
            runtime="wasm",
            task_name="demo",
        )

    def _register_node(self) -> _Node:
        private_key, public_pem = _make_keypair()
        response = self.client.post(
            "/nodes/register",
            json={"rsa_public_key_pem": public_pem, "supports_wasm": True},
            headers={"Authorization": f"Bearer {self.shared_token}"},
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        body = response.get_json()
        return _Node(body["node_id"], body["node_token"], private_key)

    def _headers(self, node: _Node) -> dict[str, str]:
        return {"X-Node-Id": node.node_id, "Authorization": f"Bearer {node.token}"}

    def _claim(self, node: _Node) -> str:
        response = self.client.post("/nodes/tasks/claim", headers=self._headers(node))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        claimed = response.get_json()
        self.assertEqual(claimed["tid"], self.tid)
        return claimed["claim_token"]

    def _submit_result(self, node: _Node, claim_token: str, result_bytes: bytes):
        return self.client.post(
            f"/nodes/tasks/{self.tid}/result",
            data=result_bytes,
            headers={
                **self._headers(node),
                "Content-Type": "application/octet-stream",
                "X-Task-Claim": claim_token,
                "X-Execution-Receipt": _signed_receipt(
                    node.private_key, self.tid, result_bytes
                ),
            },
        )

    def _submit_failure(self, node: _Node, claim_token: str, message: str):
        return self.client.post(
            f"/nodes/tasks/{self.tid}/result",
            json={"error": message},
            headers={**self._headers(node), "X-Task-Claim": claim_token},
        )

    def _billed_seconds(self) -> int:
        _, info = quota.check_quota("BILLKEY")
        return info["used"]

    def _reopen_the_claim(self, claim_token: str) -> None:
        """Put a spent claim token back on the task.

        Settling clears it, so this is how a second request that read the task
        *before* the first one finished writing still looks current -- which is
        the interleaving that used to let one task be billed twice.
        """
        redis_client.hset(f"task:{self.tid}", mapping={"claim_token": claim_token})

    def test_a_resent_result_is_recognised_and_only_billed_once(self) -> None:
        claim_token = self._claim(self.node_a)

        first = self._submit_result(self.node_a, claim_token, b"the answer")
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(first.get_json()["status"], "completed")
        billed_once = self._billed_seconds()
        self.assertGreater(billed_once, 0, "the first result should have been billed")

        # The node never saw our response, so it sends the same result again.
        second = self._submit_result(self.node_a, claim_token, b"the answer")

        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(second.get_json()["status"], "duplicate")
        self.assertEqual(self._billed_seconds(), billed_once, "billed twice")

    def test_two_results_arriving_at_once_only_settle_and_bill_once(self) -> None:
        claim_token = self._claim(self.node_a)
        self.assertEqual(
            self._submit_result(self.node_a, claim_token, b"the answer").status_code,
            200,
        )
        billed_once = self._billed_seconds()

        self._reopen_the_claim(claim_token)
        second = self._submit_result(self.node_a, claim_token, b"the answer")

        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(second.get_json()["status"], "duplicate")
        self.assertEqual(self._billed_seconds(), billed_once, "billed twice")

    def test_a_failure_report_cannot_bury_a_result_that_already_landed(self) -> None:
        claim_token = self._claim(self.node_a)
        self.assertEqual(
            self._submit_result(self.node_a, claim_token, b"the answer").status_code,
            200,
        )

        self._reopen_the_claim(claim_token)
        response = self._submit_failure(self.node_a, claim_token, "boom")

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["status"], "duplicate")

        task = task_queue.get_task(self.tid)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["error"], "")

    def test_a_made_up_claim_token_is_still_rejected(self) -> None:
        self._claim(self.node_a)

        response = self._submit_result(self.node_a, "not-a-real-token", b"the answer")

        self.assertEqual(response.status_code, 403)
        self.assertIn("Invalid claim token", response.get_json()["error"])

    def test_a_late_result_from_a_replaced_node_is_not_accepted(self) -> None:
        """The scenario behind all of this: the task moved on while A ran it."""
        first_token = self._claim(self.node_a)

        task_queue.requeue_task(self.tid, self.node_b.node_id)
        second_token = self._claim(self.node_b)
        self.assertEqual(
            self._submit_result(self.node_b, second_token, b"the answer").status_code,
            200,
        )
        billed_once = self._billed_seconds()

        late = self._submit_result(self.node_a, first_token, b"the answer")

        self.assertEqual(late.status_code, 403)
        self.assertEqual(self._billed_seconds(), billed_once, "billed twice")


if __name__ == "__main__":
    unittest.main()
