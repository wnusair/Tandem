"""A background thread that reclaims work from nodes that have died.

The task queue already knows how to requeue work off a dead node, but until now
that only happened when some other node happened to poll for work. If every node
died at once, nothing would ever trigger it and jobs would hang. This runs the
sweep on a timer instead, so failover happens on its own.

Only one server instance should sweep at a time (there may be several behind a
load balancer), so each pass is guarded by a short-lived Redis lock.

The same pass also settles verification groups that have been waiting too long
for their other copies, so a stranded replica can't leave a client polling
forever.
"""

from __future__ import annotations

import threading
import time

from app.extensions import redis_client
from app.utils.serve_queue import reap_stale_serve_nodes
from app.utils.task_queue import sweep_stale_work
from app.utils.verify import settle_expired_groups

# How often to run a sweep. Matches the node heartbeat cadence so a dead node is
# noticed quickly.
SWEEP_INTERVAL_SECONDS = 3.0

# The lock that keeps two instances from sweeping at once. The TTL is a safety
# net in case an instance dies mid-sweep; it's comfortably longer than a sweep.
_SWEEP_LOCK_KEY = "sweeper:lock"
_SWEEP_LOCK_TTL_SECONDS = 15


def _run_loop(app) -> None:
    while True:
        try:
            got_lock = redis_client.set(
                _SWEEP_LOCK_KEY, "1", nx=True, ex=_SWEEP_LOCK_TTL_SECONDS
            )
            if got_lock:
                try:
                    with app.app_context():
                        sweep_stale_work()
                        reap_stale_serve_nodes()
                        settle_expired_groups()
                finally:
                    redis_client.delete(_SWEEP_LOCK_KEY)
        except Exception as exc:  # keep the loop alive no matter what goes wrong
            app.logger.warning("failover sweep failed: %s", exc)
        time.sleep(SWEEP_INTERVAL_SECONDS)


def start_sweeper(app) -> threading.Thread:
    """Start the background failover sweeper as a daemon thread."""
    thread = threading.Thread(
        target=_run_loop, args=(app,), name="tandem-sweeper", daemon=True
    )
    thread.start()
    return thread
