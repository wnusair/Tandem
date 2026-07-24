"""Redundant execution: run some tasks on several nodes and compare answers.

A signed execution receipt proves which node produced a set of bytes, not that
the bytes are correct -- a dishonest node signs its garbage perfectly well. The
only way to catch that is a second opinion, so we quietly run a sampled fraction
of tasks on several nodes at once and compare what comes back.

The copies are ordinary tasks: own id, own encryption key, own blob, identical
claim response. A node has no way to tell it's being checked, which is the whole
point -- it can't behave only when someone's watching.

When the copies disagree we don't just make a note of it. The majority answer is
what the client gets, and any node that returned something else is off the
network for good.
"""

from __future__ import annotations

import logging
import pathlib
import secrets
import time
from typing import Any

from app.extensions import redis_client
from app.utils import receipts
from app.utils.task_queue import (
    decode_value,
    delete_task_keys,
    get_task,
    group_member_ids,
    now_ts,
    read_bytes,
    refresh_job_status,
    remove_file,
    select_least_loaded_node,
    write_bytes,
)
from flask import current_app

logger = logging.getLogger(__name__)

# Groups still waiting on copies, scored by when we stop waiting for them.
PENDING_GROUPS_KEY = "verify:pending"

# Three is the floor, not a preference. With two copies a disagreement tells you
# somebody lied but not which one, so there's no honest answer to fall back on
# and nobody you can fairly blame. Three lets an honest majority outvote a liar.
MIN_VERIFY_COPIES = 3

# A copy has had its say once it reports back. The client-visible task parks in
# `verifying` at that point rather than going straight to a terminal status.
REPORTED_STATUSES = {"completed", "failed", "verifying"}


# ── Settings ────────────────────────────────────────────────────────────────


def _config_int(key: str, default: int) -> int:
    try:
        return int(current_app.config.get(key, default))
    except (TypeError, ValueError):
        return default


def sample_percent() -> int:
    """What share of tasks get checked, 0-100. Zero turns the whole thing off."""
    return max(0, min(100, _config_int("VERIFY_SAMPLE_PERCENT", 0)))


def verify_copies() -> int:
    """How many nodes run a sampled task, counting the original."""
    return max(MIN_VERIFY_COPIES, _config_int("VERIFY_COPIES", MIN_VERIFY_COPIES))


def settle_timeout_seconds() -> int:
    """How long we wait for the other copies before giving up on them."""
    return max(1, _config_int("VERIFY_TIMEOUT_SECONDS", 300))


# ── Planning ────────────────────────────────────────────────────────────────


def plan_verification_replicas(
    planned_tasks: list[dict[str, Any]], available_nodes: list[str]
) -> list[dict[str, Any]]:
    """Duplicate a sampled fraction of the planned tasks onto other nodes.

    Returns a new list with the extra copies mixed in. Sampled tasks get a
    `verify_group` stamped on them; the copies additionally get
    `verify_replica`, which is what keeps them out of the job's task list.
    """
    percent = sample_percent()
    copies = verify_copies()

    # Not enough of the network is up to form a majority, so checking would only
    # produce answers we can't act on. Run everything the normal way.
    if percent <= 0 or len(available_nodes) < copies:
        return planned_tasks

    expanded: list[dict[str, Any]] = []
    node_load: dict[str, int] = {}

    for planned_task in planned_tasks:
        expanded.append(planned_task)

        # secrets rather than random so a node can't predict which of its work
        # is being checked and cheat on the rest.
        if secrets.randbelow(100) >= percent:
            continue

        others = [
            node_id
            for node_id in available_nodes
            if node_id != planned_task.get("assigned_node")
        ]
        if len(others) < copies - 1:
            continue

        verify_group = f"vg_{secrets.token_hex(8)}"
        planned_task["verify_group"] = verify_group

        for _ in range(copies - 1):
            replica = dict(planned_task)
            replica["verify_replica"] = True
            replica["assigned_node"] = select_least_loaded_node(others, node_load)
            # Each copy goes to a different node, or they'd be checking nothing.
            others = [n for n in others if n != replica["assigned_node"]]
            expanded.append(replica)

    return expanded


# ── Settling a group ────────────────────────────────────────────────────────


def _is_replica(task: dict[str, str]) -> bool:
    return bool(task.get("verify_replica"))


def on_task_settled(tid: str) -> None:
    """Called whenever a node reports on a task, with a result or an error."""
    task = get_task(tid)
    verify_group = (task.get("verify_group") or "").strip()
    if not verify_group:
        return

    # The first copy back starts the clock. The others get a bounded amount of
    # time to weigh in before we stop making the client wait on them.
    redis_client.zadd(
        PENDING_GROUPS_KEY,
        {verify_group: time.time() + settle_timeout_seconds()},
        nx=True,
    )

    members = [get_task(member_tid) for member_tid in group_member_ids(verify_group)]
    if all(member.get("status") in REPORTED_STATUSES for member in members if member):
        settle_group(verify_group)


def settle_group(verify_group: str) -> None:
    """Compare the copies in a group and act on what they say."""
    members = [get_task(tid) for tid in group_member_ids(verify_group)]
    members = [member for member in members if member]

    primary = next(
        (member for member in members if not _is_replica(member)), None
    )
    if primary is None:
        # The task the client was reading is gone (expired, or the job was
        # cleaned up), so there's nothing left to rule on.
        _forget_group(verify_group, members)
        return

    # Only copies that actually produced output get a vote. A copy that errored
    # out has nothing to say, and a broken node isn't the same as a lying one.
    voters = [member for member in members if member.get("output_hash")]

    if len(voters) < 2:
        _finish(primary["tid"], "inconclusive")
        _forget_group(verify_group, members)
        return

    buckets: dict[str, list[dict[str, str]]] = {}
    for voter in voters:
        buckets.setdefault(voter["output_hash"], []).append(voter)

    ranked = sorted(buckets.values(), key=len, reverse=True)
    if len(ranked) > 1 and len(ranked[1]) == len(ranked[0]):
        # Nothing has more agreement behind it than anything else. We know
        # somebody lied, but not who, so there's no trustworthy answer to hand
        # over and nobody we can fairly punish. Fail it rather than guess.
        logger.error(
            "Verification group %s could not agree on a result for task %s",
            verify_group,
            primary["tid"],
        )
        _finish(
            primary["tid"],
            "disputed",
            error="Result verification failed: nodes returned conflicting results",
        )
        _forget_group(verify_group, members)
        return

    winners = ranked[0]
    winning_hash = winners[0]["output_hash"]

    # Anything the majority disagrees with is either tampering or a badly broken
    # node. Either way it has no business running other people's work.
    for voter in voters:
        if voter["output_hash"] == winning_hash:
            continue

        node_id = (voter.get("assigned_node") or "").strip()
        if node_id:
            receipts.ban_node(
                node_id,
                f"result mismatch on task {voter.get('tid')} "
                f"(verification group {verify_group})",
            )

    if primary.get("output_hash") == winning_hash:
        _finish(primary["tid"], "verified")
    elif _repair(primary, winners[0]):
        # The copy the client was about to read is the wrong one, so swap in
        # what the majority computed before anybody sees it.
        logger.warning(
            "Repaired task %s from verification group %s", primary["tid"], verify_group
        )
        _finish(primary["tid"], "corrected")
    else:
        _finish(
            primary["tid"],
            "disputed",
            error="Result verification failed: agreed result was unavailable",
        )

    _forget_group(verify_group, members)


def _repair(primary: dict[str, str], winner: dict[str, str]) -> bool:
    """Replace the client-visible result with the one the majority agreed on."""
    winning_bytes = read_bytes(winner.get("result_path"))
    if winning_bytes is None:
        return False

    result_path = primary.get("result_path")
    if not result_path:
        return False

    write_bytes(pathlib.Path(result_path), winning_bytes)
    redis_client.hset(
        f"task:{primary['tid']}",
        mapping={"output_hash": winner.get("output_hash") or ""},
    )
    return True


def _finish(tid: str, verdict: str, *, error: str = "") -> None:
    """Move the client-visible task out of `verifying` into its real status.

    It only lands on `completed` if there are result bytes behind it -- its own,
    or the ones we just repaired it with.
    """
    task = get_task(tid)
    if not task:
        return

    if not error and not (task.get("result_path") and task.get("output_hash")):
        # Nothing ever produced a result for this one; keep whatever the node
        # reported as the reason.
        error = task.get("error") or "Task produced no result"

    redis_client.hset(
        f"task:{tid}",
        mapping={
            "status": "failed" if error else "completed",
            "error": error,
            "verify_status": verdict,
            "updated_at": now_ts(),
        },
    )

    job_id = task.get("job_id") or ""
    if job_id:
        refresh_job_status(job_id)


def _forget_group(verify_group: str, members: list[dict[str, str]]) -> None:
    """Throw away everything the group was holding on to.

    Members defer their payload cleanup while a group is live (the comparison
    may still need it), so this is where it finally happens.
    """
    for member in members:
        tid = member.get("tid") or ""
        if not tid:
            continue

        remove_file(member.get("blob_path"))
        delete_task_keys(tid)

        if _is_replica(member):
            # Nobody reads a replica's result, and dropping its task key means a
            # copy still running somewhere gets a clean 404 instead of leaving a
            # blob behind that nothing would ever clean up.
            remove_file(member.get("result_path"))
            redis_client.delete(f"task:{tid}")

    redis_client.delete(f"verify:{verify_group}:members")
    redis_client.zrem(PENDING_GROUPS_KEY, verify_group)


def settle_expired_groups() -> None:
    """Settle groups whose other copies took too long, so no client hangs.

    A replica can get stranded -- its node dies and nothing else can decrypt it,
    say -- and without this the task it was checking would sit in `verifying`
    forever.
    """
    expired = redis_client.zrangebyscore(PENDING_GROUPS_KEY, 0, time.time()) or []

    for raw_group in expired:
        verify_group = str(decode_value(raw_group))
        try:
            settle_group(verify_group)
        except Exception:
            logger.warning(
                "Could not settle verification group %s", verify_group, exc_info=True
            )
            redis_client.zrem(PENDING_GROUPS_KEY, verify_group)
