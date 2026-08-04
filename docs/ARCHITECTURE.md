# Tandem Architecture & Usage Guide

Tandem is a tool that lets you run Python functions across multiple computers. You mark functions in your code that are slow or resource-heavy, and Tandem handles compiling, packaging, and sending them to worker machines to execute.

---

## High-Level Concept

Think of Tandem as a distributed task runner:
1. **You write Python code** and mark slow functions with `@tandem.compute`.
2. **A central Server** receives your tasks and coordinates who should run them.
3. **Worker Nodes** (which could be your local machine or other computers) ask the server for work, run the tasks in a safe environment, and return the results.

All of the components below are already written and included in the repository.

---

## The Parts of Tandem

### 1. What You Use to Write Code
* **Python SDK (`sdk/python-sdk/`)**: The library you import in your Python scripts. You use its decorators (like `@tandem.compute`) to mark tasks.

### 2. What You Run in the Terminal
* **CLI (`cli/`)**: The `tandem` command-line tool. You use this to deploy projects, log in, view status, and start or stop workers.

### 3. What Runs Behind the Scenes
* **Server (`server/`)**: A Flask web server that coordinates the cluster. It holds the queue of tasks, registers user accounts, and assigns tasks to online workers.
* **Node (`node/`)**: A Rust helper program that runs on worker machines. It checks the server for tasks, executes them inside a WebAssembly container to keep them isolated from the host OS, and returns the results.
* **Compiler Engine (`tandem-compile`)**: A Rust utility that turns your Python functions into WebAssembly (`.wasm`) files during the build phase.

### 4. External Infrastructure You Must Run
* **Redis**: A fast, temporary data store. The server uses it to track which nodes are online, queue tasks, and manage active logins. You must have Redis running on your machine.
* **Database**: SQLite (which saves to a local file) runs automatically. For production, you can configure Postgres.

---

## How Code Runs (Execution Flow)

Here is exactly what happens when you run a task:
1. **Build**: You run `tandem build`. The CLI turns your Python tasks into compiled `.wasm` files.
2. **Submit**: You run `tandem start`. The CLI uploads the configuration and `.wasm` files to the server.
3. **Assign**: The server adds the tasks to a queue in Redis and matches them to available worker nodes.
4. **Execute**: The worker node claims the task, downloads the `.wasm` file, runs it securely, and generates a proof showing the code executed successfully.
5. **Collect**: The node posts the result and proof back to the server. The server verifies the proof, updates usage quotas, and stores the result.
6. **Download**: Your CLI polls the server, downloads the finished results, and displays them.

---

## Security & Trust Boundaries

To make sure tasks run safely and results can be trusted, Tandem uses several security gates:
* **Worker Registration**: Before a node can claim any work, it must prove its identity to the server using a secret registration token or a logged-in user account.
* **Task Encryption**: The server can encrypt task files so that only the specifically assigned worker node is able to decrypt and run them.
* **Result Verification**: When workers return a result, they must also provide a cryptographic proof showing they actually ran the code with the correct inputs. If the proof is missing or invalid, the server rejects the result.
* **User Accounts & Sessions**: Developers log in using a username and password. The CLI requests a short-lived access token (JWT) from the server and stores it securely in your operating system's credential store.

---

## CLI Command Reference (Ordered by Typical Usage)

When using Tandem for the first time, you will generally run commands in this sequence:

### Setup & Account Management
* `tandem auth register`  
  Creates a new developer account on the server and logs you in.
* `tandem auth login`  
  Logs into your existing Tandem account. Your credentials are saved securely in your operating system's password manager.
* `tandem settings set-server-url`  
  Configures the CLI to talk to your server (for example, `http://127.0.0.1:6767` for local testing).
* `tandem sdk install`  
  Installs the Tandem Python SDK library into your current Python environment so you can import it in your scripts.

### Creating & Managing Projects
* `tandem init`  
  Creates a starter `tandem.toml` configuration file in your directory to define your project name and entry files.
* `tandem build`  
  Compiles your Python tasks into safe WebAssembly files in your build directory.
* `tandem deploy`  
  Registers your project on the server and assigns it a unique deployment ID.

### Running Workers & Jobs
* `tandem node start`  
  Starts a worker node on your machine so it can help run tasks.
* `tandem status`  
  Displays whether you are logged in and checks if your worker node is running successfully.
* `tandem start`  
  Compiles your tasks, uploads them to the server, distributes them to active workers, and prints the results when finished.
* `tandem node logs`  
  Prints the recent log output from your worker node to troubleshoot errors.

### Web App Hosting & Cleanup
* `tandem serve`  
  Packages and deploys a web application to be hosted across your worker nodes.
* `tandem serve list`  
  Lists all of your active web applications and tells you which nodes are hosting them.
* `tandem serve stop`  
  Shuts down and removes a running web application deployment.
* `tandem clean`  
  Deletes temporary WebAssembly files and folders created during the build phase.
* `tandem auth logout`  
  Logs out of the server and removes your credentials from your machine.
* `tandem uninstall`  
  Completely removes Tandem, its background worker, and all configurations from your system.
