# Tandem CLI

This folder contains the **active Python CLI** for Tandem.

The important bit is: after installing it, you run **`tandem`** directly.
You do **not** need to run `python cli/main.py` or `python cli/cli.py` anymore.

## Install the command

### Linux / macOS

The way in is the installer script from the repo root -- it sets up its own
private Python environment and puts `tandem` on your PATH, so you don't need to
know anything about Python packaging:

```bash
./install.sh
```

Re-run it any time to pick up changes to the CLI or node -- that's the supported
way to install and update.

### Windows

`install.sh` is the Linux/macOS installer; on Windows use its twin, `install.bat`.
From a Command Prompt in the repo root:

```bat
install.bat
```

It sets up a private Python environment, puts a `tandem` command on your PATH, and
installs the node (building it if you have Rust). Re-run it any time to update.

Then verify it:

```bash
tandem --help
```

The CLI now bundles the runtime SDKs it knows about. Today that means a Python
project with `runtime = "python"` can be inspected and built without needing a
separate repo-relative SDK checkout. If you need to point at a custom SDK copy,
set `project.sdk_path` in `tandem.toml` (the legacy `project.sdk_python_path`
key still works too).

## Repo-local note

This repo also contains some older Node/TypeScript CLI scaffolding files in the
same folder. The current working path for the WASM pipeline is the **Python CLI**
under `cli/`.

## Commands

All commands that take a configuration path now default to checking `tandem.toml` or reading from the `TANDEM_CONFIG_PATH` environment variable if omitted. The CLI also automatically loads any `.env` file present in the current working directory, so your API keys are automatically picked up.

```bash
tandem init
tandem init [config_path] --name <project-name> --entry <python-entry-file>
tandem inspect [config_path]
tandem manifest [config_path]
tandem build [config_path]
tandem auth register --username <username>
tandem auth login --username <username>
tandem auth logout
tandem auth status
tandem settings show
tandem settings set-server-url <url>
tandem settings reset-server-url
tandem settings set-registration-token <token>
tandem settings reset-registration-token
tandem sdk list
tandem sdk install [name]
tandem sdk download [name] --output <dir>
tandem status
tandem usage
tandem serve [config_path]
tandem serve list
tandem serve stop <pid>
tandem node start
tandem node stop
tandem node restart
tandem node status
tandem node logs [--lines N]
tandem node enable
tandem node disable
tandem deploy [config_path]
tandem start [config_path]
tandem clean [config_path]
tandem uninstall
```

`tandem uninstall` completely removes Tandem from the machine -- it stops the
node, clears your saved login, and deletes `~/.tandem` and the `tandem` command.
Because it's destructive, it prints a random 6-digit code you have to type back
before it does anything.

`tandem status` shows whether you're logged in and whether your node is running.
The `tandem node` commands run the compute node in the background: `start`
registers it the first time and launches it, `enable` upgrades that to an OS
service that runs 24/7 (starts on boot, restarts on crash), and `disable` turns
that back off. `deploy` and `start` refuse to run unless the node is up -- set
`TANDEM_SKIP_NODE_CHECK=1` to bypass that in CI.

`tandem settings set-server-url <url>` saves a server URL so you don't need
`--server-url` on every command -- it applies across the board (auth, sdk,
deploy, start), not just to whichever command you happened to run it from.
`tandem settings show` tells you the URL currently in effect and why (a saved
setting, an environment variable, or a built-in default), and `tandem settings
reset-server-url` removes the saved override. An explicit `--server-url` flag
always wins over the saved setting.

`tandem sdk` commands require `tandem auth login` (or `register`) first --
they ask the server what SDKs and versions are available. The `name` argument
is optional: it auto-selects the SDK when there's only one available, and
lists your options if there's more than one. `tandem sdk install` is the
normal case -- it installs straight into whatever Python environment you
currently have active (a virtualenv if one's active, otherwise whatever
`python3` is on your PATH), so `import tandem` works right after. `tandem sdk
download` is for when you just want the source files without installing them.

Note this is a different `tandem` package than the one bundled inside the CLI
for build-time task discovery (mentioned above) -- `tandem sdk install` gives
you the standalone SDK meant for writing your own task code against.

`tandem init`  works interactively by default: it asks a few quick questions, shows defaults, and lets you press Enter to accept them.

Interactive defaults:

- config path: `tandem.toml`
- project name: current directory name
- entry file: `tasks.py`
- version: `0.1.0`
- output directory: `.tandem_build/<project-name>`

## Full local flow

This is the smallest end-to-end flow for the current repo.

### 1. Start the server dependencies

You need Redis running.

#### Linux / macOS

```bash
redis-server
```

#### Windows

Use Redis in WSL, Docker, or a local Redis install, then make sure the server can
reach it.

### 2. Start the Flask server

From the repo root:

#### Linux / macOS

```bash
export REDIS_URL=redis://127.0.0.1:6379/0
python server/run.py
```

#### Windows (PowerShell)

```powershell
$env:REDIS_URL = "redis://127.0.0.1:6379/0"
py server/run.py
```

The server listens on `http://127.0.0.1:6767` by default.

You don't need to set a node registration token -- if `TANDEM_NODE_REGISTRATION_TOKEN`
isn't set, the server generates a random one on this first run, saves it to
`server/keys/node_registration_token.txt` (reused on every restart from then on),
and prints it. Only set the env var yourself if you want a fixed, predictable
value instead (see `.env.example`).

### 3. Install the CLI and node

From the repo root:

#### Linux / macOS

```bash
./install.sh
```

This installs the `tandem` command and builds/installs the node binary. Re-run it
any time to update.

#### Windows

From a Command Prompt in the repo root:

```bat
install.bat
```

Like `install.sh`, this installs the `tandem` command and the node binary
(building it with Cargo if Rust is present, otherwise telling you how to drop in a
prebuilt `tandem-node.exe`).

### 4. Start your node

Point the CLI at your server and start the node. It registers itself the first
time and keeps running in the background:

```bash
tandem settings set-server-url http://127.0.0.1:6767
tandem node start
tandem status
```

If your server enforces a registration token, save it once and every future
`tandem node start` sends it automatically -- no more exporting it by hand each
session. `./install.sh` / `install.bat` already do this for you -- they check
this repo's `.env`, and then the token the server auto-generated at
`server/keys/node_registration_token.txt` -- so if you started the server in
step 2 before installing the CLI, most people following this guide won't need
to run this manually:

```bash
tandem settings set-registration-token <token>
tandem node start
```

`tandem settings show` reports which token (if any) is currently in effect and
where it came from. `TANDEM_NODE_REGISTRATION_TOKEN` still works too, if you'd
rather export it as a one-off (handy in CI):

```bash
export TANDEM_NODE_REGISTRATION_TOKEN=meow-secret   # PowerShell: $env:TANDEM_NODE_REGISTRATION_TOKEN = "meow-secret"
tandem node start
```

To run the node 24/7 (start on boot, restart on crash) instead of just for this
session:

```bash
tandem node enable
```

### 5. Create a user and store credentials locally

From the directory where you want Tandem to create or update `.env`:

#### Linux / macOS

```bash
tandem auth register --username demo --server-url http://127.0.0.1:6767
```

#### Windows (PowerShell)

```powershell
tandem auth register --username demo --server-url http://127.0.0.1:6767
```

The CLI prompts for your password securely, registers the user, authenticates, and stores your credentials in the OS keyring.

To authenticate an existing user instead:

```bash
tandem auth login --username demo --server-url http://127.0.0.1:6767
```

If you need a fresh API key, rotate it explicitly:

```bash
tandem auth login --username demo --rotate-api-key
```

Security notes:

- prefer the interactive password prompt over `--password`
- the CLI never stores your password or API key in `.env`
- user credentials are saved securely in your operating system's password manager (keyring)
- on POSIX systems it tightens `.env` permissions to owner read/write only

### 6. Build and run the sample project

From the repo root:

```bash
tandem start cli/test.toml
```

That command will:

1. inspect the Python entry file,
2. build the `.wasm` artifacts,
3. create a deployment if needed,
4. upload the TOML + manifest + wasm files,
5. wait for node execution,
6. print the returned results.

If you want to just create the deployment first:

```bash
tandem deploy cli/test.toml
```

If you already have a deployment pid and want to reuse it:

```bash
tandem start cli/test.toml --pid <deployment-pid>
```

If you want to queue the job without waiting:

```bash
tandem start cli/test.toml --no-wait
```

That prints the `job_token`, status URL, and results URL.
