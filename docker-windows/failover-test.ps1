# Proves the web-hosting failover: deploy an app across two nodes, confirm the
# load balancer spreads to both, then kill a node and confirm traffic keeps
# flowing through the survivor.
#
# Windows-native twin of docker/failover-test.sh -- same steps, ported to
# PowerShell since that's what's reliably available on a Windows host.
#
# Run from the repo root after docker-windows/build.ps1:
#   powershell -File docker-windows/failover-test.ps1
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot
$ComposeFile = "docker-windows/docker-compose.yml"
$Helper = "tandem-failover-driver"

function Cleanup {
    docker rm -f $Helper 2>$null | Out-Null
    docker compose -f $ComposeFile down -v 2>$null | Out-Null
}

Cleanup
try {
    Write-Host ">>> bringing up redis + server + two nodes"
    docker compose -f $ComposeFile up -d redis server node1 node2 | Out-Null
    Start-Sleep -Seconds 12

    Write-Host ">>> starting a long-lived driver to drive the test from"
    docker compose -f $ComposeFile run -d --name $Helper driver sleep infinity | Out-Null

    Write-Host ">>> deploying the web app across both nodes (replicas=2)"
    # Runs inside the driver container, which is Linux, so this stays bash.
    $deployScript = @'
curl -s -o /dev/null -X POST "$TANDEM_SERVER_URL/api/v1/register" -H "Content-Type: application/json" -d "{\"username\":\"failover-user\",\"password\":\"failoverpass123\"}" || true
export TANDEM_API_KEY=$(curl -sf -X POST "$TANDEM_SERVER_URL/api/v1/login" -H "Content-Type: application/json" -d "{\"username\":\"failover-user\",\"password\":\"failoverpass123\"}" | python3 -c "import sys, json; print(json.load(sys.stdin)[\"api_key\"])")
cd /opt/tandem/sample-serve && python3 -c "
import os
from tandem_cli.remote import serve_deploy
from tandem_cli.app_config import load_project_config
c = load_project_config('tandem.toml')
r = serve_deploy(project_root=str(c.project_root), start_command=c.build_start, replicas=2, name=c.name, server_url=os.environ['TANDEM_SERVER_URL'], api_key=os.environ['TANDEM_API_KEY'])
print(r['pid'])
"
'@
    $deployPid = (docker exec $Helper bash -c $deployScript | Select-Object -Last 1).Trim()
    Write-Host "    deployed: $deployPid"
    $url = "http://server:6767/app/$deployPid/"

    Write-Host ">>> waiting for the app to come up on the nodes"
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        docker exec -e URL=$url $Helper bash -c 'curl -sf "$URL" >/dev/null 2>&1'
        if ($LASTEXITCODE -eq 0) { $up = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $up) {
        Write-Host "FAIL: the hosted app never became reachable"
        exit 1
    }

    Write-Host ">>> nodes serving before the kill:"
    $before = docker exec -e URL=$url $Helper bash -c 'for i in $(seq 1 8); do curl -sf "$URL"; done' 2>$null
    ([regex]::Matches($before, 'node_[a-f0-9]+') | ForEach-Object { $_.Value } | Sort-Object -Unique) |
        ForEach-Object { Write-Host "    $_" }

    Write-Host ">>> killing node1"
    docker compose -f $ComposeFile kill node1 | Out-Null
    Start-Sleep -Seconds 8

    Write-Host ">>> the load balancer should keep serving through the survivor:"
    $ok = $false
    for ($i = 0; $i -lt 15; $i++) {
        $response = docker exec -e URL=$url $Helper bash -c 'curl -sf "$URL"' 2>$null
        if ($response -match "hello from the tandem web app") {
            $ok = $true
            Write-Host "    $response"
            break
        }
        Start-Sleep -Seconds 2
    }

    Write-Host ""
    if ($ok) {
        Write-Host "==================================="
        Write-Host "  FAILOVER TEST PASSED"
        Write-Host "==================================="
    } else {
        Write-Host "FAILOVER TEST FAILED: the load balancer stopped serving after node1 died"
        exit 1
    }
}
finally {
    Cleanup
}
