"""Signed execution receipts and node reputation.

Verifies the RSA-PSS signed receipt a compute node submits with each result, and
tracks bad-receipt counts in Redis so nodes that keep failing get banned.

Worth being clear about what a receipt does and doesn't prove -- the name of the
game here is a plain digital signature, not a proof of computation. It shows that
*this* node produced *these* exact bytes (we re-derive the output hash ourselves)
and that nobody altered them in transit. It says nothing about whether the bytes
are the right answer, or how much work actually ran -- a dishonest node hashes
and signs its garbage just as correctly as an honest one signs real work.
Catching a wrong answer takes a second opinion, which is what `app.utils.verify`
does; the real cost of a task is metered by the server, not read off the receipt.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, utils as asym_utils

from app.extensions import redis_client
from app.models import NodePublicKey
from app.utils.task_queue import drain_node_queue

logger = logging.getLogger(__name__)

BAD_RECEIPT_THRESHOLD = 5

# Fingerprints of the public keys belonging to nodes we've banned.
BANNED_KEYS_SET = "banned:keys"


def build_receipt_message(
    tid: str, instruction_count: int, memory_hash: str, output_hash: str
) -> bytes:
    """Build the canonical byte string that gets signed.

    Format: ``"{tid}|{instruction_count}|{memory_hash}|{output_hash}"``
    """
    return f"{tid}|{instruction_count}|{memory_hash}|{output_hash}".encode("utf-8")


def verify_receipt(
    receipt_json: dict, result_bytes: bytes, node_id: str
) -> tuple[bool, str]:
    """Verify an execution receipt against the result payload and node key.

    Returns ``(True, "")`` on success or ``(False, "error reason")`` on failure.
    """
    # 1. Look up the node's RSA public key
    node_key_row = NodePublicKey.query.filter_by(node_id=node_id).first()
    if node_key_row is None:
        return False, f"No registered public key for node {node_id}"

    # 2. Verify output_hash matches actual result bytes
    expected_output_hash = hashlib.sha256(result_bytes).hexdigest()
    receipt_output_hash = (receipt_json.get("output_hash") or "").strip()
    if receipt_output_hash != expected_output_hash:
        return False, (
            f"output_hash mismatch: receipt says {receipt_output_hash}, "
            f"actual is {expected_output_hash}"
        )

    # 3. Verify the RSA-PSS signature
    tid = receipt_json.get("tid", "")
    instruction_count = receipt_json.get("instruction_count", 0)
    memory_hash = receipt_json.get("memory_hash", "")
    output_hash = receipt_output_hash

    message = build_receipt_message(tid, instruction_count, memory_hash, output_hash)
    signature_b64 = receipt_json.get("signature", "")

    try:
        signature = base64.b64decode(signature_b64)
    except Exception:
        return False, "Could not base64-decode the signature"

    try:
        public_key = serialization.load_pem_public_key(
            node_key_row.rsa_public_key_pem.encode("utf-8")
        )
    except Exception:
        return False, "Failed to load stored PEM public key"

    try:
        public_key.verify(
            signature,
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return False, "RSA-PSS signature verification failed"
    except Exception as exc:
        return False, f"Signature verification error: {exc}"

    # 4. Signature checks out. The receipt carries an instruction_count too, but
    #    it's node-reported and we don't bill on it -- quota is metered from the
    #    server's own clock (see app.utils.quota). It stays in the signed message
    #    only so the signature covers everything the node claimed.
    return True, ""


def _banned_flag_key(node_id: str) -> str:
    return f"node:{node_id}:banned"


def public_key_fingerprint(rsa_public_key_pem: str) -> str:
    """A short stable id for a node's RSA key.

    We ban the fingerprint alongside the node id so a banned node can't just
    call /nodes/register again and come back with a fresh id.
    """
    return hashlib.sha256(rsa_public_key_pem.strip().encode("utf-8")).hexdigest()


def is_node_banned(node_id: str) -> bool:
    """Has this node been kicked off the network?"""
    return bool(redis_client.exists(_banned_flag_key(node_id)))


def is_public_key_banned(rsa_public_key_pem: str) -> bool:
    """Does this public key belong to a node we already banned?"""
    fingerprint = public_key_fingerprint(rsa_public_key_pem)
    return bool(redis_client.sismember(BANNED_KEYS_SET, fingerprint))


def ban_node(node_id: str, reason: str) -> None:
    """Kick a node off the network for good.

    Dropping it from the `nodes` set on its own doesn't stick -- the node's very
    next heartbeat re-adds itself -- so we also set a flag that every node
    request checks, and remember its key fingerprint so re-registering with the
    same identity doesn't quietly let it back in.
    """
    redis_client.set(_banned_flag_key(node_id), reason or "banned")

    # Hand off whatever it was still sitting on before dropping it from the
    # `nodes` set -- once it's out of that set the failover sweep stops looking
    # at it, and anything left in its queue would just be stranded.
    drain_node_queue(node_id)
    redis_client.srem("nodes", node_id)

    key_row = NodePublicKey.query.filter_by(node_id=node_id).first()
    if key_row is not None:
        # ponytail: fingerprint ban only stops the same key coming back. A node
        # that generates a fresh keypair still gets a new identity -- closing
        # that needs attestation or stake, which is a much bigger change.
        redis_client.sadd(
            BANNED_KEYS_SET, public_key_fingerprint(key_row.rsa_public_key_pem)
        )

    logger.warning("Node %s banned: %s", node_id, reason)


def penalize_node(node_id: str, reason: str) -> int:
    """Count one bad receipt against a node, and ban it if they keep coming.

    A receipt that won't verify might just be a bug or a version skew, so these
    get a few strikes before we act. Proven result tampering is different --
    that calls `ban_node` straight away, no strikes.
    """
    count = redis_client.incr(f"node:{node_id}:bad_receipts")
    if count >= BAD_RECEIPT_THRESHOLD:
        ban_node(node_id, f"{count} bad receipts (latest: {reason})")
    return count
