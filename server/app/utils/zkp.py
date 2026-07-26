"""Fiat-Shamir execution receipt verifier.

Verifies RSA-PSS signed execution receipts submitted by compute nodes and
tracks bad-receipt counts in Redis for node reputation management.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.extensions import redis_client
from app.models import NodePublicKey

BAD_RECEIPT_THRESHOLD = 5


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

    # 4. Verification succeeded – instruction_count is available for quota tracking
    return True, ""


def increment_bad_receipt_count(node_id: str) -> int:
    """Increment and return the bad receipt count from Redis."""
    return redis_client.incr(f"node:{node_id}:bad_receipts")
