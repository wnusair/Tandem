#!/usr/bin/env bash
#
# Proves the web-hosting failover: deploy an app across two nodes, confirm the
# load balancer spreads to both, then kill a node and confirm traffic keeps
# flowing through the survivor.
#
# Run from the repo root after docker/build.sh:
#   bash docker/failover-test.sh
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
COMPOSE="docker compose -f docker/docker-compose.yml"
HELPER="tandem-failover-driver"

cleanup() {
  docker rm -f "$HELPER" >/dev/null 2>&1 || true
  $COMPOSE down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

echo ">>> bringing up redis + server + two nodes"
$COMPOSE up -d redis server node1 node2 >/dev/null 2>&1
sleep 12

echo ">>> starting a long-lived driver to drive the test from"
$COMPOSE run -d --name "$HELPER" driver sleep infinity >/dev/null 2>&1

echo ">>> deploying the web app across both nodes (replicas=2)"
PID=$(docker exec "$HELPER" bash -c '
curl -s -o /dev/null -X POST "$TANDEM_SERVER_URL/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"failover-user\",\"password\":\"failoverpass123\"}" || true
export TANDEM_API_KEY=$(curl -sf -X POST "$TANDEM_SERVER_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"failover-user\",\"password\":\"failoverpass123\"}" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)[\"api_key\"])")
cd /opt/tandem/sample-serve && python3 -c "
import os
from tandem_cli.remote import serve_deploy
from tandem_cli.app_config import load_project_config
c = load_project_config(\"tandem.toml\")
r = serve_deploy(project_root=str(c.project_root), start_command=c.build_start, replicas=2,
                 name=c.name, server_url=os.environ[\"TANDEM_SERVER_URL\"],
                 api_key=os.environ[\"TANDEM_API_KEY\"])
print(r[\"pid\"])
"' | tr -d '\r')
echo "    deployed: $PID"
URL="http://server:6767/app/$PID/"

echo ">>> waiting for the app to come up on the nodes"
for _ in $(seq 1 40); do
  docker exec "$HELPER" curl -sf "$URL" >/dev/null 2>&1 && break
  sleep 2
done

echo ">>> nodes serving before the kill:"
docker exec "$HELPER" bash -c "for _ in \$(seq 1 8); do curl -sf $URL; done" 2>/dev/null \
  | grep -oE 'node_[a-f0-9]+' | sort -u | sed 's/^/    /'

echo ">>> killing node1"
$COMPOSE kill node1 >/dev/null 2>&1
sleep 8

echo ">>> the load balancer should keep serving through the survivor:"
ok=0
for _ in $(seq 1 15); do
  response=$(docker exec "$HELPER" curl -sf "$URL" 2>/dev/null || true)
  if echo "$response" | grep -q "hello from the tandem web app"; then
    ok=1
    echo "    $response"
    break
  fi
  sleep 2
done

echo ""
if [ "$ok" = "1" ]; then
  echo "==================================="
  echo "  FAILOVER TEST PASSED"
  echo "==================================="
else
  echo "FAILOVER TEST FAILED: the load balancer stopped serving after node1 died"
  exit 1
fi
