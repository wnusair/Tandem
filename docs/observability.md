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
