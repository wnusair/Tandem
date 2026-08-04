#!/usr/bin/env bash
#
# The end-to-end test, run from inside the driver container. It plays a developer
# on a fresh machine: log in, build a task, run it on the nodes, and check usage.
set -euo pipefail

SERVER="${TANDEM_SERVER_URL:-http://server:6767}"
USERNAME="e2e-user"
PASSWORD="e2e-password-123"

say() { echo ""; echo ">>> $*"; }

say "waiting for the server at $SERVER"
for _ in $(seq 1 60); do
  if curl -sf "$SERVER/" >/dev/null 2>&1; then break; fi
  sleep 1
done

say "register + login"
# Register may 409 on a re-run; that's fine, we just need to log in afterwards.
curl -s -o /dev/null -X POST "$SERVER/api/v1/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" || true

API_KEY="$(curl -sf -X POST "$SERVER/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["api_key"])')"
export TANDEM_API_KEY="$API_KEY"
echo "got api key: ${API_KEY:0:8}..."

say "build the sample task (compile Python -> WASM component)"
tandem build

say "run compute on a node: crunch(5) should return 10"
RESULT=""
for attempt in $(seq 1 20); do
  if RESULT="$(python3 -c 'import tandem; from app import crunch; print(crunch.submit(5).result(timeout=120))' 2>/tmp/compute.err)"; then
    break
  fi
  echo "  attempt $attempt: not ready yet ($(tail -n1 /tmp/compute.err 2>/dev/null))"
  RESULT=""
  sleep 3
done

echo "compute result: '$RESULT'"
if [ "$RESULT" != "10" ]; then
  echo "FAIL: expected 10, got '$RESULT'"
  cat /tmp/compute.err 2>/dev/null || true
  exit 1
fi

say "run several concurrently and gather them (spreads across nodes)"
python3 - <<'PY'
import tandem
from app import crunch

futures = [crunch.submit(n) for n in (10, 100, 1000)]
results = tandem.gather(*futures)
print("gathered:", results)

expected = [45, 4950, 499500]
assert results == expected, f"expected {expected}, got {results}"
print("gather OK")
PY

say "web hosting: deploy an app across the nodes and load-balance to it"
PID="$(cd /opt/tandem/sample-serve && python3 -c '
import os
from tandem_cli.remote import serve_deploy
from tandem_cli.app_config import load_project_config
c = load_project_config("tandem.toml")
r = serve_deploy(
    project_root=str(c.project_root), start_command=c.build_start, replicas=2, name=c.name,
    server_url=os.environ["TANDEM_SERVER_URL"], api_key=os.environ["TANDEM_API_KEY"],
)
print(r["pid"])
')"
echo "serve deployment id: $PID"
APP_URL="$SERVER/app/$PID/"

echo "waiting for the hosted app to come up on the nodes..."
UP=""
for _ in $(seq 1 40); do
  if curl -sf "$APP_URL" >/dev/null 2>&1; then UP="yes"; break; fi
  sleep 2
done
if [ -z "$UP" ]; then
  echo "FAIL: the hosted app never became reachable"
  exit 1
fi

echo "one response through the load balancer:"
curl -sf "$APP_URL"
curl -sf "$APP_URL" | grep -q "hello from the tandem web app" || {
  echo "FAIL: unexpected app response"
  exit 1
}

echo "hitting the load balancer 12 times to see traffic spread across nodes..."
SERVING_NODES="$(for _ in $(seq 1 12); do curl -sf "$APP_URL" 2>/dev/null; done | grep -oE 'node_[a-f0-9]+' | sort -u)"
echo "distinct nodes that served traffic:"
echo "$SERVING_NODES"
DISTINCT="$(printf '%s\n' "$SERVING_NODES" | grep -c 'node_')"
if [ "$DISTINCT" -lt 2 ]; then
  echo "FAIL: expected the load balancer to spread across 2 nodes, saw $DISTINCT"
  exit 1
fi
echo "load balancing confirmed across $DISTINCT nodes"

say "tandem usage"
tandem usage --server-url "$SERVER" --api-key "$API_KEY"

echo ""
echo "==================================="
echo "  TANDEM END-TO-END TEST PASSED"
echo "==================================="
