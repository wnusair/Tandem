import base64
import os
import tempfile
import unittest

os.environ["TANDEM_DISABLE_SWEEPER"] = "1"
os.environ.setdefault("TANDEM_NODE_REGISTRATION_TOKEN", "shared-token")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp_db.name}")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db, redis_client  # noqa: E402
from app.utils.task_queue import create_task, requeue_task  # noqa: E402


def _make_keypair():
    """A node's real RSA keypair: private key stays 'on the node', PEM goes up."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_key, public_pem


class TaskEncryptionFailoverTests(unittest.TestCase):
    """An encrypted task that fails over to a different node must still be
    decryptable by whoever ends up holding it -- the DEK is wrapped for every
    node, not pinned to the one it was first assigned to."""

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

    def _register_node(self, public_pem: str) -> tuple[str, str]:
        response = self.client.post(
            "/nodes/register",
            json={"rsa_public_key_pem": public_pem},
            headers={"Authorization": f"Bearer {self.shared_token}"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        return body["node_id"], body["node_token"]

    def _download_and_decrypt(
        self, tid: str, node_id: str, node_token: str, private_key
    ) -> bytes:
        """Do exactly what a node does: pull the blob, unwrap the DEK with its
        own private key, and AES-GCM-decrypt the payload."""
        task = redis_client.hgetall(f"task:{tid}")
        download_token = task[b"download_token"].decode()

        response = self.client.get(
            f"/nodes/tasks/{tid}/download/{download_token}",
            headers={"X-Node-Id": node_id, "Authorization": f"Bearer {node_token}"},
        )
        self.assertEqual(response.status_code, 200)

        dek_b64 = response.headers.get("X-Task-Dek-Encrypted")
        iv_b64 = response.headers.get("X-Task-IV")
        self.assertIsNotNone(dek_b64, "node got no wrapped DEK -- undecryptable job")
        self.assertIsNotNone(iv_b64)

        dek = private_key.decrypt(
            base64.b64decode(dek_b64),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return AESGCM(dek).decrypt(base64.b64decode(iv_b64), response.get_data(), None)

    def _claim_for(self, node_id: str, node_token: str) -> str:
        """Claim a task so the node becomes its holder with a fresh download token."""
        response = self.client.post(
            "/nodes/tasks/claim",
            json={"node_id": node_id},
            headers={"X-Node-Id": node_id, "Authorization": f"Bearer {node_token}"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["tid"]

    def test_failover_target_can_decrypt(self) -> None:
        priv_a, pem_a = _make_keypair()
        priv_b, pem_b = _make_keypair()
        node_a, token_a = self._register_node(pem_a)
        node_b, token_b = self._register_node(pem_b)

        payload = b"the secret task payload that must survive a failover"
        tid = create_task(
            job_id="job_test",
            pid="pid_test",
            name="enc-test",
            filename="task.bin",
            payload=payload,
            assigned_node=node_a,
        )

        # Node A can decrypt it (the happy path still works).
        tid_a = self._claim_for(node_a, token_a)
        self.assertEqual(tid_a, tid)
        self.assertEqual(
            self._download_and_decrypt(tid, node_a, token_a, priv_a), payload
        )

        # Now A "dies" and the task fails over to B. Before the fix, B would get
        # A's wrapped DEK and fail to decrypt.
        requeue_task(tid, node_b)
        tid_b = self._claim_for(node_b, token_b)
        self.assertEqual(tid_b, tid)
        self.assertEqual(
            self._download_and_decrypt(tid, node_b, token_b, priv_b), payload
        )

    def test_unencrypted_when_no_keys(self) -> None:
        # No node has registered a public key, so the blob is stored in the
        # clear and served without a DEK header rather than as an undecryptable
        # ciphertext.
        payload = b"plaintext fallback payload"
        tid = create_task(
            job_id="job_plain",
            pid="pid_plain",
            name="plain",
            filename="task.bin",
            payload=payload,
            assigned_node=None,
        )

        node_id, node_token = self._register_node(_make_keypair()[1])
        # Point the node at the already-created plaintext task.
        redis_client.hset(f"task:{tid}", "assigned_node", node_id)
        redis_client.rpush(f"node:{node_id}:queue", tid)
        tid_claimed = self._claim_for(node_id, node_token)
        self.assertEqual(tid_claimed, tid)

        task = redis_client.hgetall(f"task:{tid}")
        download_token = task[b"download_token"].decode()
        response = self.client.get(
            f"/nodes/tasks/{tid}/download/{download_token}",
            headers={"X-Node-Id": node_id, "Authorization": f"Bearer {node_token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("X-Task-Dek-Encrypted"))
        self.assertEqual(response.get_data(), payload)


if __name__ == "__main__":
    unittest.main()
