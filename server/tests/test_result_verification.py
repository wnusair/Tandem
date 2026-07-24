import base64
import hashlib
import json
import os
import tempfile
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
from app.utils import quota, receipts, task_queue, verify  # noqa: E402


# What a node looks like from the test's side: its id, its bearer token, and the
# private key it signs execution receipts with.
_Node = namedtuple("_Node", "node_id token private_key")


def _make_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_key, public_pem


def _sign_receipt(
    private_key, tid: str, result_bytes: bytes, instruction_count: int = 4242
) -> str:
    """Build the execution receipt exactly the way the Rust node does.

    A dishonest node signs its made-up result just as correctly as an honest
    one signs real work, which is the whole reason redundancy has to exist -- so
    the tests sign every result properly, lies included. The instruction_count is
    settable so a test can sign an absurd figure and prove billing ignores it.
    """
    output_hash = hashlib.sha256(result_bytes).hexdigest()
    memory_hash = hashlib.sha256(b"").hexdigest()

    message = f"{tid}|{instruction_count}|{memory_hash}|{output_hash}".encode("utf-8")
    signature = private_key.sign(
        message,
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


class ResultVerificationTests(unittest.TestCase):
    """Running a task on several nodes at once and comparing what comes back.

    A signed receipt only proves who produced a set of bytes, so the only way to
    catch a node returning nonsense is to have somebody else do the same work.
    """

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

        # Check everything, so the tests don't depend on a dice roll.
        self.app.config["VERIFY_SAMPLE_PERCENT"] = 100
        self.app.config["VERIFY_COPIES"] = 3
        self.app.config["VERIFY_TIMEOUT_SECONDS"] = 300

        self.nodes = [self._register_node() for _ in range(3)]
        self.job = task_queue.create_job(
            pid="pid_test", name="verify-test", metadata={}, total_tasks=1
        )
        self.job_id = self.job["job_id"]

    # ── Node plumbing ───────────────────────────────────────────────────────

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
        return {
            "X-Node-Id": node.node_id,
            "Authorization": f"Bearer {node.token}",
        }

    def _run(self, node: _Node, result_bytes: bytes) -> str:
        """Claim whatever is waiting for this node and report a result for it."""
        claim = self.client.post("/nodes/tasks/claim", headers=self._headers(node))
        self.assertEqual(claim.status_code, 200, claim.get_data(as_text=True))
        claimed = claim.get_json()
        tid = claimed["tid"]

        response = self.client.post(
            f"/nodes/tasks/{tid}/result",
            data=result_bytes,
            headers={
                **self._headers(node),
                "Content-Type": "application/octet-stream",
                "X-Task-Claim": claimed["claim_token"],
                "X-Execution-Receipt": _sign_receipt(
                    node.private_key, tid, result_bytes
                ),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return tid

    # ── Job plumbing ────────────────────────────────────────────────────────

    def _queue_task(self) -> list[str]:
        """Plan one task the way /start does, then create whatever came back."""
        planned = [
            {
                "filename": "task.wasm",
                "payload": b"the work to be done",
                "assigned_node": self.nodes[0].node_id,
                "runtime": "wasm",
                "task_name": "demo",
            }
        ]
        expanded = verify.plan_verification_replicas(
            planned, [node.node_id for node in self.nodes]
        )

        return [
            task_queue.create_task(
                job_id=self.job_id,
                pid="pid_test",
                name="verify-test",
                filename=entry["filename"],
                payload=entry["payload"],
                assigned_node=entry["assigned_node"],
                runtime=entry["runtime"],
                task_name=entry["task_name"],
                verify_group=entry.get("verify_group", ""),
                verify_replica=entry.get("verify_replica", False),
            )
            for entry in expanded
        ]

    def _primary_tid(self) -> str:
        """The one task the client can actually see."""
        visible = task_queue.get_job_task_ids(self.job_id)
        self.assertEqual(len(visible), 1, "replicas leaked into the job's task list")
        return visible[0]

    def _is_banned(self, node: _Node) -> bool:
        """Ask the way a node would: does the server still take its requests?"""
        response = self.client.post("/nodes/tasks/claim", headers=self._headers(node))
        return response.status_code == 403

    def _expire_group(self, primary_tid: str) -> None:
        """Push a group's deadline into the past, as if its copies took too long."""
        verify_group = task_queue.get_task(primary_tid)["verify_group"]
        self.assertTrue(verify_group)
        redis_client.zadd(verify.PENDING_GROUPS_KEY, {verify_group: 0})

    # ── The checks ──────────────────────────────────────────────────────────

    def test_replicas_are_hidden_and_agreement_passes(self) -> None:
        tids = self._queue_task()
        self.assertEqual(len(tids), 3, "one task should have been fanned out to three")

        primary_tid = self._primary_tid()
        self.assertEqual(primary_tid, tids[0])

        # Every node computes the same thing, which is what should normally
        # happen. The task shouldn't finish until they've all weighed in.
        self._run(self.nodes[0], b"the right answer")
        self.assertEqual(task_queue.get_task(primary_tid)["status"], "verifying")
        self.assertFalse(task_queue.refresh_job_status(self.job_id)["done"])

        self._run(self.nodes[1], b"the right answer")
        self._run(self.nodes[2], b"the right answer")

        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "completed")
        self.assertEqual(primary["verify_status"], "verified")

        summary = task_queue.refresh_job_status(self.job_id)
        self.assertTrue(summary["done"])
        self.assertEqual(summary["total_tasks"], 1)
        self.assertEqual(summary["status"], "completed")

        # The client still gets exactly one result, with the right bytes.
        results = task_queue.get_job_results(self.job_id)
        self.assertEqual(len(results), 1)
        self.assertEqual(base64.b64decode(results[0]["result_b64"]), b"the right answer")
        self.assertEqual(results[0]["verify_status"], "verified")

    def test_lying_replica_is_banned(self) -> None:
        self._queue_task()
        primary_tid = self._primary_tid()

        self._run(self.nodes[0], b"the right answer")
        self._run(self.nodes[1], b"the right answer")
        self._run(self.nodes[2], b"a made up answer")

        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "completed")
        self.assertEqual(primary["verify_status"], "verified")

        results = task_queue.get_job_results(self.job_id)
        self.assertEqual(base64.b64decode(results[0]["result_b64"]), b"the right answer")

        # The odd one out is off the network; the honest two carry on.
        self.assertTrue(self._is_banned(self.nodes[2]))
        self.assertFalse(self._is_banned(self.nodes[0]))
        self.assertFalse(self._is_banned(self.nodes[1]))

    def test_lying_primary_gets_its_result_repaired(self) -> None:
        """The worst case: the copy the client reads is the dishonest one."""
        self._queue_task()
        primary_tid = self._primary_tid()

        self._run(self.nodes[0], b"a made up answer")
        self._run(self.nodes[1], b"the right answer")
        self._run(self.nodes[2], b"the right answer")

        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "completed")
        self.assertEqual(primary["verify_status"], "corrected")

        # The client never sees the lie -- the majority's bytes were swapped in.
        results = task_queue.get_job_results(self.job_id)
        self.assertEqual(base64.b64decode(results[0]["result_b64"]), b"the right answer")
        self.assertTrue(self._is_banned(self.nodes[0]))

    def test_total_disagreement_fails_the_task_and_blames_nobody(self) -> None:
        self._queue_task()
        primary_tid = self._primary_tid()

        self._run(self.nodes[0], b"answer one")
        self._run(self.nodes[1], b"answer two")
        self._run(self.nodes[2], b"answer three")

        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "failed")
        self.assertEqual(primary["verify_status"], "disputed")
        self.assertIn("verification failed", primary["error"])

        # Somebody lied, but there's no majority to say who, so nobody is
        # punished on a guess.
        for node in self.nodes:
            self.assertFalse(self._is_banned(node))

    def test_a_banned_node_cannot_re_register_with_the_same_key(self) -> None:
        self._queue_task()
        self._run(self.nodes[0], b"the right answer")
        self._run(self.nodes[1], b"the right answer")
        self._run(self.nodes[2], b"a made up answer")

        public_pem = self.nodes[2].private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

        response = self.client.post(
            "/nodes/register",
            json={"rsa_public_key_pem": public_pem, "supports_wasm": True},
            headers={"Authorization": f"Bearer {self.shared_token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_banning_a_node_hands_off_its_queued_work(self) -> None:
        """A ban drops the node from the `nodes` set, which is also what the
        failover sweep walks -- so the ban has to re-home its queue itself or
        that work would sit there forever."""
        waiting = task_queue.create_task(
            job_id=self.job_id,
            pid="pid_test",
            name="verify-test",
            filename="other.wasm",
            payload=b"unrelated work",
            assigned_node=self.nodes[0].node_id,
            runtime="wasm",
            task_name="unrelated",
        )

        receipts.ban_node(self.nodes[0].node_id, "test")

        moved = task_queue.get_task(waiting)
        self.assertEqual(moved["status"], "queued")
        self.assertIn(
            moved["assigned_node"], {self.nodes[1].node_id, self.nodes[2].node_id}
        )

    def test_a_ban_survives_the_next_heartbeat(self) -> None:
        """Dropping a node from the `nodes` set alone never stuck -- its next
        heartbeat put it straight back."""
        receipts.ban_node(self.nodes[0].node_id, "test")

        response = self.client.post(
            "/nodes/health", json={}, headers=self._headers(self.nodes[0])
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.nodes[0].node_id, task_queue.get_all_node_ids())

    def test_a_stalled_group_still_settles(self) -> None:
        """A replica that never reports can't leave a client polling forever."""
        self._queue_task()
        primary_tid = self._primary_tid()

        self._run(self.nodes[0], b"the right answer")
        self._run(self.nodes[1], b"the right answer")
        # Node 2 never comes back.

        self.assertEqual(task_queue.get_task(primary_tid)["status"], "verifying")

        self._expire_group(primary_tid)
        verify.settle_expired_groups()

        # Two copies still agree, so the answer stands on their say-so.
        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "completed")
        self.assertEqual(primary["verify_status"], "verified")
        self.assertTrue(task_queue.refresh_job_status(self.job_id)["done"])

    def test_a_single_answer_is_not_treated_as_verified(self) -> None:
        """One copy agreeing with itself proves nothing."""
        self._queue_task()
        primary_tid = self._primary_tid()

        self._run(self.nodes[0], b"the right answer")

        self._expire_group(primary_tid)
        verify.settle_expired_groups()

        primary = task_queue.get_task(primary_tid)
        self.assertEqual(primary["status"], "completed")
        self.assertEqual(primary["verify_status"], "inconclusive")

    def test_failover_never_puts_two_copies_on_one_node(self) -> None:
        """If a copy moves after its node dies, it has to move somewhere that
        isn't already running a copy -- otherwise a dishonest node could end up
        confirming its own answer."""
        # Registered before the task exists so it gets a wrapped key like the
        # others and is a genuine candidate.
        spare = self._register_node()

        self._queue_task()
        primary_tid = self._primary_tid()

        claim = self.client.post(
            "/nodes/tasks/claim", headers=self._headers(self.nodes[0])
        )
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(claim.get_json()["tid"], primary_tid)

        # Node 0 goes quiet mid-task and the sweeper reclaims its work.
        redis_client.hset(f"node:{self.nodes[0].node_id}", "last_seen", "1")
        task_queue.sweep_stale_work()

        # Nodes 1 and 2 are each running a replica, so the spare is the only
        # node left that this copy can legitimately go to.
        self.assertEqual(
            task_queue.get_task(primary_tid)["assigned_node"], spare.node_id
        )

    def test_off_by_default(self) -> None:
        self.app.config["VERIFY_SAMPLE_PERCENT"] = 0
        tids = self._queue_task()

        self.assertEqual(len(tids), 1)
        self.assertEqual(task_queue.get_task(tids[0])["verify_group"], "")

        self._run(self.nodes[0], b"the right answer")
        self.assertEqual(task_queue.get_task(tids[0])["status"], "completed")
        self.assertTrue(task_queue.refresh_job_status(self.job_id)["done"])

    def test_billing_uses_server_time_not_the_receipts_number(self) -> None:
        """A node signs its own instruction_count, so it could put anything there.
        We bill the seconds the server watched the task run, so a forged figure
        never reaches the quota."""
        # One plain primary task, no cross-checking copies to muddy the billing.
        self.app.config["VERIFY_SAMPLE_PERCENT"] = 0

        # A deployment ties the task's pid to an API key -- the quota bucket.
        user = User(username="biller", password="unused")
        db.session.add(user)
        db.session.flush()
        db.session.add(
            Deployment(name="bill", pid="pid_test", user_id=user.id, api_key="BILLKEY")
        )
        db.session.commit()

        tid = task_queue.create_task(
            job_id=self.job_id,
            pid="pid_test",
            name="verify-test",
            filename="task.wasm",
            payload=b"the work to be done",
            assigned_node=self.nodes[0].node_id,
            runtime="wasm",
            task_name="demo",
        )

        # Claiming stamps claimed_at = now; rewind it five seconds so the task
        # looks like it genuinely occupied the node for a measurable stretch.
        claim = self.client.post(
            "/nodes/tasks/claim", headers=self._headers(self.nodes[0])
        )
        self.assertEqual(claim.status_code, 200, claim.get_data(as_text=True))
        claimed = claim.get_json()
        self.assertEqual(claimed["tid"], tid)
        rewound = float(task_queue.get_task(tid)["claimed_at"]) - 5.0
        redis_client.hset(f"task:{tid}", "claimed_at", str(rewound))

        # The node reports a billion instructions. Billing must not care.
        result_bytes = b"the right answer"
        response = self.client.post(
            f"/nodes/tasks/{tid}/result",
            data=result_bytes,
            headers={
                **self._headers(self.nodes[0]),
                "Content-Type": "application/octet-stream",
                "X-Task-Claim": claimed["claim_token"],
                "X-Execution-Receipt": _sign_receipt(
                    self.nodes[0].private_key, tid, result_bytes,
                    instruction_count=10**9,
                ),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        _, info = quota.check_quota("BILLKEY")
        # Charged on our clock -- about five seconds, nowhere near the billion.
        self.assertGreaterEqual(info["used"], 5)
        self.assertLess(info["used"], 60)

    def test_too_few_nodes_to_form_a_majority(self) -> None:
        """Two nodes can disagree but can't name a liar, so don't bother."""
        planned = [
            {
                "filename": "task.wasm",
                "payload": b"the work to be done",
                "assigned_node": self.nodes[0].node_id,
                "runtime": "wasm",
                "task_name": "demo",
            }
        ]
        expanded = verify.plan_verification_replicas(
            planned, [self.nodes[0].node_id, self.nodes[1].node_id]
        )
        self.assertEqual(len(expanded), 1)
        self.assertNotIn("verify_group", expanded[0])


if __name__ == "__main__":
    unittest.main()
