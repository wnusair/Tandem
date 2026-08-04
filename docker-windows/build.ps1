# Build the docker images for the Windows e2e stack. Unlike docker/build.sh,
# this doesn't build the Rust binaries first -- Dockerfile.node and
# Dockerfile.driver compile them inside the Docker build itself, so all this
# needs is Docker Desktop.
$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

Write-Host ">>> building docker images"
docker compose -f docker-windows/docker-compose.yml build

Write-Host ""
Write-Host "Done. Run the end-to-end test with:"
Write-Host "  docker compose -f docker-windows/docker-compose.yml up --abort-on-container-exit --exit-code-from driver"
