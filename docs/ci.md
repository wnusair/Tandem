# CI checks

Every push and pull request runs the checks in `.github/workflows/checks.yml`
(wired up via `.github/workflows/ci.yml`). A release build (`.github/workflows/release.yml`)
runs the exact same checks on the tagged commit before it packages anything,
so a broken build or a failing test can't ship.

Here's how to run each check locally before you push.

## Python lint

```bash
pip install ruff
ruff check cli server sdk/python-sdk --select F --extend-exclude cli/_bundled --extend-exclude cli/build
```

Scoped to pyflakes (`F`) -- unused imports, undefined names, that kind of
thing -- not the full stylistic rule set, since the codebase has never been
auto-formatted. `cli/_bundled` is a vendored copy of the SDK, kept in sync by
hand (see `cli/tests/test_sdk_bundle_sync.py`), and `cli/build` is a build
artifact directory, so both are skipped.

## Python tests

You need a Redis instance for the server tests. Easiest way is Docker:

```bash
docker run -d --rm -p 6379:6379 redis:7-alpine
```

Then, from the repo root:

```bash
pip install -r requirements.txt
pip install -e ./cli
pip install -e ./sdk/python-sdk
pip install pytest

REDIS_URL=redis://127.0.0.1:6379/15 pytest server/tests cli/tests sdk/python-sdk/tests
```

## Rust lint

```bash
cargo clippy --manifest-path node/Cargo.toml --all-targets -- -D warnings
cargo clippy --manifest-path sdk/Cargo.toml --all-targets -- -D warnings
```

## Rust tests

```bash
cargo test --manifest-path node/Cargo.toml
cargo test --manifest-path sdk/Cargo.toml
```

The node's sandbox tests shell out to `bwrap` (bubblewrap) and just skip
themselves if it isn't installed, so they'll run either way -- install
`bubblewrap` if you want them to actually exercise the sandbox instead of
no-op.

## End-to-end

The full stack test in `docker/` -- server + two nodes + a driver that builds
and runs a real task, deploys a web app, and checks load balancing and
failover. Needs Docker and Rust:

```bash
bash docker/build.sh
docker compose -f docker/docker-compose.yml up --abort-on-container-exit --exit-code-from driver
```

See `docker/docker-compose.yml` for what each service does, and
`docker/failover-test.sh` for the separate node-failover scenario (not run in
CI -- it's a slower, more manual check for when you're touching failover
logic specifically).
