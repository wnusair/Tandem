# Tandem Python SDK

Tandem lets you write normal Python, mark the parts you want to run across your
machines, and then run them. Those parts can run as one-off compute jobs or as a
hosted web app. This is a quick guide to the SDK and the `tandem` commands.

For a deeper walkthrough with a use case and example for each part of the SDK,
see [GUIDE.md](GUIDE.md). For runnable examples, see [examples/](examples/).

## Install

Install Tandem with the install script. This puts the `tandem` command and the
node on your PATH:

```bash
./install.sh          # Windows: install.bat
```

That gives you the CLI and the node, which is all you need for `tandem build`,
`tandem start`, and `tandem serve`. If you also want to `import tandem` in your
own scripts (needed for `.submit()`, below), install the Python package into
your environment:

```bash
tandem sdk install
```

## Log in

```bash
tandem auth login
```

Your credentials live in your operating system's keyring, and every `tandem`
command uses them automatically. Nothing is written to a file.

## Write a task

A task is a plain function with a marker on it. Put your tasks in a `.py` file:

```python
import tandem

@tandem.compute(timeout_ms=5000)
def crunch(n=1000):
    total = 0
    for i in range(n):
        total += i * i
    return total
```

A few things to know:

- Calling `crunch(5)` runs it right here, locally. Calling `crunch.submit(5)`
  runs it on a node and hands you back a future (see below).
- `timeout_ms` is how long the task may run on a node. A loop needs enough of it
  or it gets cut short, so set it for anything that isn't instant.
- Give your parameters defaults (`n=1000`). `tandem start` runs every task once
  with no arguments, so a task without defaults can't be run that way.
- Each node runs its own frozen copy of your code, so a task can read
  module-level values but can't change shared state. Mutating a global raises
  `TandemValidationError`. Pass data in as arguments and return a result instead.

## The SDK, piece by piece

Here's the whole surface at a glance. [GUIDE.md](GUIDE.md) covers each one with a
use case and a worked example.

| Thing | What it does |
|---|---|
| `@tandem.compute(...)` | Marks a function as a task. A bare call runs locally; `.submit(args)` runs it on a node. |
| `task.submit(*args)` | Sends one call to a node and returns a `ComputeFuture` right away, without blocking. |
| `future.done()` | `True` if the result is ready. Doesn't block. |
| `future.result(timeout=None)` | Waits for and returns the result. Raises if the task failed. |
| `tandem.gather(*futures)` | Waits for several futures and returns their results, in order. |
| `tandem.split(fn, chunk=8)` | Returns a function that applies `fn` to each item of a list. `chunk` hints how many items go to each node. |
| `tandem.Immutable(value)` | A read-only constant. Read it with `.value`; assigning to it raises. |
| `tandem.describe_target(module)` | Lists the tasks Tandem found in a module. Handy for a quick check. |
| `TandemValidationError` | Raised when a task does something it can't, like mutating shared state. |

## Run your tasks

Two ways, depending on what you want.

From the command line, with no arguments:

```bash
tandem build          # compile your tasks
tandem start          # run every task once on your nodes and print the results
```

From your own Python, with arguments. `.submit()` reads your API key from the
environment, so print it once and export it first:

```bash
tandem auth login --show-api-key       # prints "API key: <key>"
export TANDEM_API_KEY=<key>
python3 your_script.py
```

```python
future = crunch.submit(1000)
print(future.result(timeout=60))
print(tandem.gather(crunch.submit(10), crunch.submit(100)))
```

## Host a web app

If your project is a web app instead of one-off compute, host it on your nodes:

```bash
tandem serve web.toml
tandem serve list             # what's running
tandem serve stop <pid>       # take one down
```

See [examples/web_example.py](examples/web_example.py) for a Flask app that does
this, including how to bundle its dependencies.

## Command reference

| Command | What it does |
|---|---|
| `tandem auth register` | Create an account and log in. |
| `tandem auth login` | Log in. Add `--show-api-key` to print your key for `TANDEM_API_KEY`. |
| `tandem auth status` | Show who you're logged in as. |
| `tandem auth logout` | Log out and clear your credentials. |
| `tandem init` | Create a starter `tandem.toml` in the current folder. |
| `tandem build` | Compile your tasks to `.wasm`. Local, no server needed. |
| `tandem deploy` | Register a deployment on the server and print its pid. |
| `tandem start` | Build, deploy, and run every task once (no arguments) on your nodes, then print the results. |
| `tandem serve <toml>` | Host a web app on your nodes, behind the load balancer. |
| `tandem serve list` | List your running web deployments. |
| `tandem serve stop <pid>` | Stop and remove a web deployment. |
| `tandem node start` | Start the background node on this machine so it can run work. |
| `tandem node stop` / `restart` | Stop or restart the node. |
| `tandem node status` / `logs` | Show the node's status or its logs. |
| `tandem status` | Show your login and whether the node is running. |
| `tandem settings` | View or change local settings, like which server to talk to. |
| `tandem sdk list` / `install` / `download` | Browse, install, or copy the Tandem SDK from the server. |
| `tandem usage` | Show how much of your resource limits you're using. |
| `tandem clean` | Remove build artifacts. |
