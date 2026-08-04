"""Per-account resource usage: what an account is using, and against what limit.

This is scaffolding. Today only compute time (seconds the server measured) is
really measured; RAM, storage, CPU, and GPU are placeholders with clear limits,
sitting behind the same interface so they can be filled in later without
changing any callers or the `tandem usage` output.

Limits are per user *account*. A user may hold several API keys, so usage is
summed across all of a user's keys.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.extensions import db
from app.models import UserAPI
from app.utils import quota
from app.utils.task_queue import get_all_node_ids, get_node, safe_int

# Per-account limits. Real enforcement comes later; for now these are just the
# numbers `tandem usage` shows percentages against. They're deliberately simple
# constants so they're easy to move to per-account config or a DB column later.
ACCOUNT_COMPUTE_LIMIT_SECONDS = quota.QUOTA_DEFAULT_LIMIT  # compute seconds, rolling 24h
ACCOUNT_RAM_LIMIT_BYTES = 5 * 2**30                    # 5 GiB
ACCOUNT_STORAGE_LIMIT_BYTES = 5 * 2**30                # 5 GiB
ACCOUNT_CPU_LIMIT_CORES = 4                            # placeholder
ACCOUNT_GPU_LIMIT_COUNT = 1                            # placeholder

# `source` values: whether `used` is a real measurement or a not-yet-wired stub.
MEASURED = "measured"
PLACEHOLDER = "placeholder"


@dataclass(frozen=True)
class ResourceMetric:
    """How much of one resource an account is using, and its ceiling.

    `source` says whether `used` is a real measurement or a placeholder that
    still needs wiring up, so `tandem usage` can be honest about which is which.
    """

    type: str
    used: float
    limit: float
    unit: str
    source: str

    @property
    def percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return round(min(self.used / self.limit * 100.0, 100.0), 1)

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "used": self.used,
            "limit": self.limit,
            "unit": self.unit,
            "percent": self.percent,
            "source": self.source,
        }


def _account_api_keys(user_id: int) -> list[str]:
    statement = select(UserAPI.api_key).where(UserAPI.user_id == user_id)
    return list(db.session.scalars(statement).all())


def _nodes_for_user(user_id: int) -> list[dict]:
    """Every registered node owned by this account.

    Nodes only remember an owner when they registered with a user's API key
    (see nodes.py's register()); token-based nodes stay anonymous and never
    show up here. This is a plain linear scan -- fine at the node counts this
    project runs at today.
    """
    owner_id = str(user_id)
    nodes = [get_node(node_id) for node_id in get_all_node_ids()]
    return [node for node in nodes if node.get("owner_user_id") == owner_id]


def _collect_compute(user_id: int) -> ResourceMetric:
    """Real: sum the rolling compute-seconds usage across the account's API keys."""
    used = 0
    for api_key in _account_api_keys(user_id):
        _, info = quota.check_quota(api_key)
        used += int(info.get("used", 0))
    return ResourceMetric(
        type="compute",
        used=float(used),
        limit=float(ACCOUNT_COMPUTE_LIMIT_SECONDS),
        unit="seconds",
        source=MEASURED,
    )


def _collect_cpu(user_id: int) -> ResourceMetric:
    """Real once you've registered a node: total CPU cores across your nodes,
    reported at registration time (see registration.rs). Falls back to the
    placeholder limit if no node has reported cores yet."""
    total_cores = sum(safe_int(node.get("cpu_cores")) for node in _nodes_for_user(user_id))
    if total_cores <= 0:
        return ResourceMetric(
            type="cpu", used=0.0, limit=float(ACCOUNT_CPU_LIMIT_CORES), unit="cores", source=PLACEHOLDER
        )
    return ResourceMetric(type="cpu", used=0.0, limit=float(total_cores), unit="cores", source=MEASURED)


def _collect_ram(user_id: int) -> ResourceMetric:
    """Real once you've registered a node: total memory across your nodes.
    Falls back to the placeholder limit if no node has reported memory yet."""
    total_mb = sum(safe_int(node.get("memory_mb")) for node in _nodes_for_user(user_id))
    if total_mb <= 0:
        return ResourceMetric(
            type="ram", used=0.0, limit=float(ACCOUNT_RAM_LIMIT_BYTES), unit="bytes", source=PLACEHOLDER
        )
    return ResourceMetric(
        type="ram", used=0.0, limit=float(total_mb * 2**20), unit="bytes", source=MEASURED
    )


def usage_for_user(user_id: int) -> list[ResourceMetric]:
    """Gather every resource metric for one account.

    Compute time, CPU, and RAM are real once a node's registered; storage and
    GPU are still placeholders -- a fixed limit with 0 used -- until wired up.
    This is also the order `tandem usage` prints them in.
    """

    def placeholder(resource_type: str, limit: float, unit: str) -> ResourceMetric:
        return ResourceMetric(
            type=resource_type,
            used=0.0,
            limit=float(limit),
            unit=unit,
            source=PLACEHOLDER,
        )

    return [
        _collect_compute(user_id),
        _collect_ram(user_id),
        placeholder("storage", ACCOUNT_STORAGE_LIMIT_BYTES, "bytes"),
        _collect_cpu(user_id),
        placeholder("gpu", ACCOUNT_GPU_LIMIT_COUNT, "gpus"),
    ]
