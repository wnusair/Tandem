# Tandem

Tandem is a distributed compute and hosting framework for Python. It allows you to run slow or resource-heavy functions across multiple computers, compiled securely into WebAssembly (WASM). It also includes support for hosting web applications on the same worker nodes.

## Quick Start

### 1. Install Tandem
Run the installer script in the repository root:
* **Linux / macOS**: `./install.sh`
* **Windows**: `install.bat`

This puts the `tandem` CLI tool and node worker on your path.

### 2. Log In & Setup
Ensure Redis is running, start the server (`python server/run.py`), and register your local CLI:
```bash
# Point the CLI to your local server
tandem settings set-server-url http://127.0.0.1:6767

# Register a user account
tandem auth register --username developer
```

### 3. Write a Task
Create a file named `tasks.py`:
```python
import tandem

@tandem.compute(timeout_ms=5000)
def compute_square(n=10):
    return n * n
```

### 4. Run the Job
```bash
# Start your local worker node
tandem node start

# Compile your task and run it across workers
tandem start
```

---

## Documentation Links

For details on how the system is put together and operational instructions, see:

* **[Tandem Architecture](docs/ARCHITECTURE.md)**: Explains the high-level design, components, how execution flows, and system security boundaries in plain terms.
* **[REST API Reference](docs/API_REFERENCE.md)**: Explains all server blueprints, expected request/response shapes, and authentication details.
* **[CLI Commands Reference](cli/README.md)**: Extensive documentation of all available CLI operations, local workflow examples, and parameters.
* **[Python SDK Documentation](sdk/python-sdk/README.md)**: Reference manual for all Python APIs, decorators, and parallel execution helpers.
* **[SDK Usage Guide](sdk/python-sdk/GUIDE.md)**: A detailed walkthrough with code examples for every SDK feature.

---

## License
Tandem is open-source software licensed under the Apache 2.0 License.
