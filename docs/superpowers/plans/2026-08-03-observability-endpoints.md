# Observability Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give operators an unauthenticated liveness/readiness surface plus a basic queue/latency/failure-rate metrics endpoint for `server/`, so a load balancer or monitoring agent can tell "process is up" from "process can actually serve work" without needing credentials.

**Architecture:** A new Flask blueprint (`health_bp`) adds three unauthenticated routes: `GET /healthz` (liveness — always 200 if the process can respond), `GET /readyz` (readiness — pings Redis and runs `SELECT 1` against Postgres, 503 if either fails, never echoes the exception text or a connection string), and `GET /metrics` (hand-rolled Prometheus text-exposition format — no new dependency, since the metric set is small). Queue depth is computed live from existing Redis list lengths (`tasks:unassigned*`, `node:{id}:queue`) — no new instrumentation needed there. Task latency and failure rate need new cumulative counters, added at the two places a task already transitions to a terminal state (`complete_task`/`fail_task` in `task_queue.py`) — that's the single natural insertion point, not duplicated logic scattered elsewhere.

**Tech Stack:** Flask, Flask-SQLAlchemy (`db.session.execute`), Flask-Redis (`redis_client`), SQLAlchemy's `text()`. No new dependencies.

## Global Constraints

- Every new route is genuinely unauthenticated — do not add `@require_jwt`, `require_user_api_key()`, or any node-auth decorator to `health_bp`.
- No response body may ever contain a raw exception string, `DATABASE_URL`, `REDIS_URL`, or any task payload/result — dependency failures log full detail via `logger.error(..., exc_info=True)` and return only a fixed `"ok"`/`"error"` string per dependency.
- Follow the existing test scaffold exactly (`server/tests/test_usage.py`): env vars set before importing `app`, `create_app()` + `app_context()` in `setUpClass`, `redis_client.flushdb()` + `db.drop_all()`/`db.create_all()` in `setUp`.
- Metrics are cumulative counters since the Redis instance was last flushed (not a sliding time window) — document this explicitly so it isn't mistaken for a rate.
- Commit after each task (each task is its own self-contained, independently-testable diff, in the 50–150 line range).

---

### Task 1: Queue-depth and task-outcome metrics in `task_queue.py`

**Files:**
- Modify: `server/app/utils/task_queue.py`
- Test: `server/tests/test_task_queue_metrics.py` (create)

**Interfaces:**
- Produces: `get_queue_depth() -> int`, `get_task_metrics() -> dict[str, int | float]` (keys: `completed_total`, `failed_total`, `terminal_count`, `latency_seconds_sum`) — both public, called by Task 3's blueprint code.
- Consumes: existing `redis_client`, `safe_int`, `safe_float`, `get_all_node_ids`, `now_ts` from the same module.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_task_queue_metrics.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_task_queue_metrics.py -v`
Expected: FAIL with `AttributeError: module 'app.utils.task_queue' has no attribute 'get_queue_depth'`

- [ ] **Step 3: Write minimal implementation**

In `server/app/utils/task_queue.py`, add after `get_all_node_ids` (after line 202, before `get_node`):

```python
def get_queue_depth() -> int:
    """Tasks sitting in a Redis list waiting to be claimed by a node -- the
    unassigned pools plus every node's per-node backlog. A task a node has
    already claimed is popped off its list, so this only counts backlog, not
    work already in flight."""
    depth = safe_int(redis_client.llen("tasks:unassigned"))
    depth += safe_int(redis_client.llen("tasks:unassigned:wasm"))
    for node_id in get_all_node_ids():
        depth += safe_int(redis_client.llen(f"node:{node_id}:queue"))
    return depth
```

Then, after `now_ts()` (after line 49, before `safe_float`), add the metrics counters and reader:

```python
def _record_task_terminal(created_at: str, timestamp: str, *, failed: bool) -> None:
    """Bump the cumulative task-outcome counters. Called once from complete_task
    and once from fail_task -- the only two places a task reaches a terminal
    state. Counters are cumulative since the Redis instance was last flushed,
    not a sliding window; see docs/observability.md."""
    redis_client.incr("metrics:tasks:terminal:count")
    redis_client.incr(
        "metrics:tasks:failed:count" if failed else "metrics:tasks:completed:count"
    )
    latency = max(0.0, safe_float(timestamp) - safe_float(created_at))
    redis_client.incrbyfloat("metrics:tasks:latency:seconds:sum", latency)


def get_task_metrics() -> dict[str, int | float]:
    return {
        "completed_total": safe_int(redis_client.get("metrics:tasks:completed:count")),
        "failed_total": safe_int(redis_client.get("metrics:tasks:failed:count")),
        "terminal_count": safe_int(redis_client.get("metrics:tasks:terminal:count")),
        "latency_seconds_sum": safe_float(
            redis_client.get("metrics:tasks:latency:seconds:sum")
        ),
    }
```

Note `_record_task_terminal` is defined before `safe_float`/`safe_int` exist lexically if placed right after `now_ts()` — Python resolves names at call time, not definition time, so this is fine as long as it's placed anywhere at module level before the module finishes loading. Keep it right after `now_ts()` as shown; `safe_float`/`safe_int` are defined a few lines below in the same module and will already exist by the time `_record_task_terminal` is ever *called*.

Now wire it into `complete_task` (around line 645) — insert the call right after `timestamp = now_ts()`:

```python
    timestamp = now_ts()
    _record_task_terminal(task.get("created_at", ""), timestamp, failed=False)
    redis_client.hset(
        f"task:{tid}",
```

And into `fail_task` (around line 672) — same pattern:

```python
    timestamp = now_ts()
    _record_task_terminal(task.get("created_at", ""), timestamp, failed=True)
    redis_client.hset(
        f"task:{tid}",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_task_queue_metrics.py -v`
Expected: PASS (3 tests)

Also run the full existing suite to confirm nothing broke:

Run: `pytest server/tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/utils/task_queue.py server/tests/test_task_queue_metrics.py
git commit -m "add queue depth and task outcome metrics to task_queue"
```

---

### Task 2: `/healthz` and `/readyz` endpoints

**Files:**
- Create: `server/app/blueprints/health.py`
- Modify: `server/app/__init__.py`
- Test: `server/tests/test_health.py` (create)

**Interfaces:**
- Produces: `health_bp` (Flask `Blueprint`, registered at `url_prefix="/"`), routes `GET /healthz`, `GET /readyz`.
- Consumes: `db`, `redis_client` from `app.extensions` (existing).

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_health.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_health.py -v`
Expected: FAIL with 404s (`/healthz` and `/readyz` don't exist yet)

- [ ] **Step 3: Write minimal implementation**

Create `server/app/blueprints/health.py`:

```python
"""
Unauthenticated operational endpoints for deployment monitoring.

Routes:
  GET /healthz  — liveness: is the process up and able to handle a request.
  GET /readyz   — readiness: can it also reach Redis and Postgres.
  GET /metrics  — Prometheus text-exposition metrics (added in a later change).

None of these require authentication, and none of their responses include
connection strings, credentials, or task payloads. See docs/observability.md.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from app.extensions import db, redis_client

logger = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@health_bp.route("/readyz", methods=["GET"])
def readyz():
    checks = {"redis": "ok", "postgres": "ok"}

    try:
        redis_client.ping()
    except Exception:
        logger.error("readyz: redis check failed", exc_info=True)
        checks["redis"] = "error"

    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        logger.error("readyz: postgres check failed", exc_info=True)
        checks["postgres"] = "error"
        db.session.rollback()

    ready = all(value == "ok" for value in checks.values())
    status_code = 200 if ready else 503
    return (
        jsonify({"status": "ready" if ready else "not_ready", "checks": checks}),
        status_code,
    )
```

In `server/app/__init__.py`, add the import alongside the other blueprint imports (after line 125's `from app.blueprints.index import index_bp`):

```python
    from app.blueprints.health import health_bp
```

And register it alongside the others (after line 131's `app.register_blueprint(index_bp, url_prefix="/")`):

```python
    app.register_blueprint(health_bp, url_prefix="/")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_health.py -v`
Expected: PASS (4 tests)

Run: `pytest server/tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/blueprints/health.py server/app/__init__.py server/tests/test_health.py
git commit -m "add unauthenticated healthz and readyz endpoints"
```

---

### Task 3: `/metrics` endpoint

**Files:**
- Modify: `server/app/blueprints/health.py`
- Test: `server/tests/test_health.py` (extend)

**Interfaces:**
- Consumes: `get_queue_depth`, `get_task_metrics`, `get_all_node_ids`, `get_healthy_node_ids` from `app.utils.task_queue` (Task 1).
- Produces: `GET /metrics` returning `Content-Type: text/plain; version=0.0.4; charset=utf-8`.

- [ ] **Step 1: Write the failing test**

Add to `server/tests/test_health.py` (new imports at top, alongside the existing ones):

```python
from app.utils import task_queue  # noqa: E402
```

New test methods on `HealthEndpointTests`:

```python
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
        self.assertIn("text/plain", response.content_type)

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

    def test_metrics_requires_no_auth(self) -> None:
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest server/tests/test_health.py -v`
Expected: FAIL with 404 (`/metrics` doesn't exist yet)

- [ ] **Step 3: Write minimal implementation**

In `server/app/blueprints/health.py`, change the `flask` import line to also bring in `Response`:

```python
from flask import Blueprint, Response, jsonify
```

Add an import for the task_queue helpers:

```python
from app.utils.task_queue import (
    get_all_node_ids,
    get_healthy_node_ids,
    get_queue_depth,
    get_task_metrics,
)
```

Append the rendering function and route at the end of the file:

```python
def _render_prometheus_metrics() -> str:
    queue_depth = get_queue_depth()
    nodes_total = len(get_all_node_ids())
    nodes_healthy = len(get_healthy_node_ids())
    task_metrics = get_task_metrics()
    terminal_count = task_metrics["terminal_count"]
    failure_ratio = (
        task_metrics["failed_total"] / terminal_count if terminal_count else 0.0
    )

    lines = [
        "# HELP tandem_queue_depth_tasks Tasks waiting in a Redis queue to be claimed by a node.",
        "# TYPE tandem_queue_depth_tasks gauge",
        f"tandem_queue_depth_tasks {queue_depth}",
        "# HELP tandem_nodes_total Nodes currently tracked by the server.",
        "# TYPE tandem_nodes_total gauge",
        f"tandem_nodes_total {nodes_total}",
        "# HELP tandem_nodes_healthy Nodes with a heartbeat inside the staleness window.",
        "# TYPE tandem_nodes_healthy gauge",
        f"tandem_nodes_healthy {nodes_healthy}",
        "# HELP tandem_tasks_completed_total Tasks that finished successfully, cumulative since the last Redis flush.",
        "# TYPE tandem_tasks_completed_total counter",
        f"tandem_tasks_completed_total {task_metrics['completed_total']}",
        "# HELP tandem_tasks_failed_total Tasks that finished with an error, cumulative since the last Redis flush.",
        "# TYPE tandem_tasks_failed_total counter",
        f"tandem_tasks_failed_total {task_metrics['failed_total']}",
        "# HELP tandem_task_latency_seconds_sum Sum of queue-to-terminal latency across finished tasks, in seconds.",
        "# TYPE tandem_task_latency_seconds_sum counter",
        f"tandem_task_latency_seconds_sum {task_metrics['latency_seconds_sum']:.6f}",
        "# HELP tandem_task_latency_seconds_count Finished tasks counted in tandem_task_latency_seconds_sum.",
        "# TYPE tandem_task_latency_seconds_count counter",
        f"tandem_task_latency_seconds_count {terminal_count}",
        "# HELP tandem_task_failure_ratio Fraction of finished tasks that failed, from 0 to 1.",
        "# TYPE tandem_task_failure_ratio gauge",
        f"tandem_task_failure_ratio {failure_ratio:.6f}",
        "",
    ]
    return "\n".join(lines)


@health_bp.route("/metrics", methods=["GET"])
def metrics():
    return Response(
        _render_prometheus_metrics(), mimetype="text/plain; version=0.0.4; charset=utf-8"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest server/tests/test_health.py -v`
Expected: PASS (6 tests)

Run: `pytest server/tests -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/blueprints/health.py server/tests/test_health.py
git commit -m "add /metrics endpoint with queue depth, node counts, and task outcome rates"
```

---

### Task 4: Document the endpoints

**Files:**
- Create: `docs/observability.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Write the doc**

Create `docs/observability.md`:

```markdown
# Observability: health, readiness, and metrics

The server exposes three unauthenticated endpoints for deployment monitoring.
None of them require a JWT, API key, or node token, and none of their
responses ever include connection strings, credentials, or task payloads.

## `GET /healthz` — liveness

Answers "is the process up and able to handle a request", nothing more. No
dependency checks. Use this for a process-restart probe (e.g. a container
orchestrator deciding whether to kill and restart the pod).

Always returns `200`:

```json
{"status": "ok"}
```

## `GET /readyz` — readiness

Answers "can this instance actually serve work" — pings Redis (`PING`) and
runs `SELECT 1` against Postgres. Use this for a load-balancer/traffic-routing
probe (e.g. deciding whether to send this instance requests at all).

`200` when both dependencies are reachable:

```json
{"status": "ready", "checks": {"redis": "ok", "postgres": "ok"}}
```

`503` when either is not, with the specific failure(s) narrowed down but no
exception detail or connection info in the body — the exception is logged
server-side (`logger.error(..., exc_info=True)`) for whoever is watching logs:

```json
{"status": "not_ready", "checks": {"redis": "error", "postgres": "ok"}}
```

## `GET /metrics` — queue and task metrics

Prometheus text-exposition format (`Content-Type: text/plain; version=0.0.4`),
scrapeable by Prometheus directly or any tool that understands that format.

All counters are **cumulative since the Redis instance was last flushed or
restarted** — not a sliding time window. If you need a rate over time, compute
it in your monitoring system from successive scrapes (e.g. Prometheus'
`rate()`), the same way you would for any Prometheus counter.

| Metric | Type | Unit | Meaning |
|---|---|---|---|
| `tandem_queue_depth_tasks` | gauge | tasks | Tasks sitting in a Redis queue waiting to be claimed by a node (unassigned pools + every node's per-node backlog). Does not include tasks already claimed/running. |
| `tandem_nodes_total` | gauge | nodes | Nodes currently tracked by the server (have registered at least once and haven't been forgotten). |
| `tandem_nodes_healthy` | gauge | nodes | Nodes with a heartbeat inside the staleness window (currently 5s — see `NODE_STALE_SECONDS` in `server/app/utils/task_queue.py`). |
| `tandem_tasks_completed_total` | counter | tasks | Tasks that finished successfully. |
| `tandem_tasks_failed_total` | counter | tasks | Tasks that finished with an error. |
| `tandem_task_latency_seconds_sum` | counter | seconds | Sum of queue-to-terminal latency (`completed_at - created_at`) across every finished task, successful or failed. |
| `tandem_task_latency_seconds_count` | counter | tasks | Number of finished tasks included in `tandem_task_latency_seconds_sum`. Divide the two for the cumulative average latency. |
| `tandem_task_failure_ratio` | gauge | ratio (0–1) | `tandem_tasks_failed_total / (tandem_tasks_completed_total + tandem_tasks_failed_total)`, computed fresh on every scrape. `0` if no tasks have finished yet. |

Example scrape:

```
# HELP tandem_queue_depth_tasks Tasks waiting in a Redis queue to be claimed by a node.
# TYPE tandem_queue_depth_tasks gauge
tandem_queue_depth_tasks 3
# HELP tandem_tasks_completed_total Tasks that finished successfully, cumulative since the last Redis flush.
# TYPE tandem_tasks_completed_total counter
tandem_tasks_completed_total 42
# HELP tandem_task_failure_ratio Fraction of finished tasks that failed, from 0 to 1.
# TYPE tandem_task_failure_ratio gauge
tandem_task_failure_ratio 0.066667
```

## Access control

All three routes are intentionally unauthenticated — that's the whole point,
since a load balancer's health probe or a Prometheus scraper generally can't
present a JWT or API key. This is safe because none of them expose secrets:
`/readyz` never echoes an exception message or connection string, and
`/metrics` only ever reports aggregate counts, never task contents or user
data.

That said, `/metrics` does reveal internal topology (node counts, queue
depth) to anyone who can reach the port. If you don't want that visible
outside your own infrastructure, put it behind a reverse-proxy rule or
network policy that only allows your monitoring system to reach `/metrics` —
that's a deployment-level access control, not something this server enforces
in code.
```

- [ ] **Step 2: Commit**

```bash
git add docs/observability.md
git commit -m "document healthz, readyz, and metrics endpoints"
```
