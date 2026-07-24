# Tandem REST API Reference

This document covers all HTTP endpoints supported by the Tandem server.

## Authentication Overview

The Tandem API supports three different ways to authenticate requests, depending on the endpoint:

1. **User API Key**: Send the key in the `Authorization: Bearer <API_KEY>` header. Used for deployment, starting jobs, serve operations, and usage tracking.
2. **JWT Access Token**: Send the token in the `Authorization: Bearer <JWT_ACCESS_TOKEN>` header. Used for developer account management and listing SDKs.
3. **Node Token**: Send the token in the `Authorization: Bearer <NODE_TOKEN>` header along with the `X-Node-Id: <NODE_ID>` header. Used for all worker node interactions.

---

## API Versioning Expectations

Currently, account and desktop management endpoints use versioned paths prefixed with `/api/v1`. Operational, job-based, and node endpoints (such as `/start`, `/deploy`, `/nodes`, `/serve`) use unversioned paths. Developers should use the exact paths documented below.

---

## Developer Account & Auth Endpoints

These routes require a JWT Access Token, except for `/login` and `/register` which are open.

### Register User
* **Method & Path**: `POST /api/v1/auth/register`
* **Auth**: None
* **Request Body (JSON)**:
  ```json
  {
    "username": "example_user",
    "password": "securepassword123"
  }
  ```
* **Success Response (201 Created)**:
  ```json
  {
    "status": "success",
    "message": "Account created. You can now log in."
  }
  ```
* **Common Errors**:
  - `400 Bad Request`: Missing username/password, or username contains invalid characters, or password is less than 10 characters.
  - `409 Conflict`: Username already exists.
  - `429 Too Many Requests`: Rate limit exceeded (5 attempts per minute per IP).

### Login
* **Method & Path**: `POST /api/v1/auth/login`
* **Auth**: None
* **Request Body (JSON)**:
  ```json
  {
    "username": "example_user",
    "password": "securepassword123"
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "status": "success",
    "access_token": "eyJhbGciOi...",
    "refresh_token": "eyJhbGciOi...",
    "token_type": "Bearer",
    "expires_in": 900,
    "username": "example_user",
    "api_key": "user_api_key_here"
  }
  ```
* **Common Errors**:
  - `401 Unauthorized`: Invalid credentials.
  - `429 Too Many Requests`: Rate limit exceeded.

### Refresh Token
* **Method & Path**: `POST /api/v1/auth/refresh`
* **Auth**: None (Token sent in request body)
* **Request Body (JSON)**:
  ```json
  {
    "refresh_token": "eyJhbGciOi..."
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "access_token": "eyJhbGciOi...",
    "token_type": "Bearer",
    "expires_in": 900
  }
  ```
* **Common Errors**:
  - `400 Bad Request`: `refresh_token` is required.
  - `401 Unauthorized`: Refresh token has expired or is invalid.

### Logout
* **Method & Path**: `POST /api/v1/auth/logout`
* **Auth**: None (Token sent in request body)
* **Request Body (JSON)**:
  ```json
  {
    "refresh_token": "eyJhbGciOi..."
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "status": "success",
    "message": "Logged out"
  }
  ```
* **Common Errors**:
  - `400 Bad Request`: Token is invalid or missing.

---

## Desktop & SDK Endpoints

### List SDKs
* **Method & Path**: `GET /api/v1/desktop/sdks`
* **Auth**: JWT Access Token
* **Query Parameters**:
  - `q` (Optional): String filter to search name, language, or description.
* **Success Response (200 OK)**:
  ```json
  {
    "sdks": [
      {
        "name": "tandem-python-sdk",
        "language": "Python",
        "description": "Official Python SDK...",
        "version": "0.1.0",
        "download_url": null
      }
    ]
  }
  ```

---

## Job & Deployment Endpoints

These routes require authentication using a **User API Key** (sent in the `Authorization: Bearer <API_KEY>` header).

### Create Deployment
* **Method & Path**: `POST /deploy/`
* **Auth**: User API Key
* **Request Body**: Can accept either JSON `{ "name": "project_name" }` OR multipart form-data uploading a `toml_file` containing configuration details.
* **Success Response (201 Created)**:
  ```json
  {
    "message": "Deployment Successful",
    "name": "project_name",
    "pid": "serve_or_deploy_pid_hex"
  }
  ```
* **Common Errors**:
  - `400 Bad Request`: Missing project name.
  - `401 Unauthorized`: Missing or invalid API key.

### Start Job (Queue Tasks)
* **Method & Path**: `POST /start/`
* **Auth**: User API Key
* **Request Body (Multipart Form-Data)**:
  - `toml_file` (Required): The `tandem.toml` file.
  - `pid` (Required): The deployment PID.
  - `manifest_file` (For WASM tasks): The task manifest JSON.
  - `wasm_files` (For WASM tasks): One or more `.wasm` compiled task files.
  - `pickle_files` (For legacy Python tasks): One or more serialized Python task files.
* **Success Response (202 Accepted)**:
  ```json
  {
    "message": "Tasks queued successfully",
    "pid": "serve_or_deploy_pid",
    "job_id": "job_id_uuid",
    "job_token": "job_token_secret",
    "name": "project_name",
    "task_ids": ["task_uuid_1", "task_uuid_2"],
    "counts": {
      "queued": 2,
      "running": 0,
      "completed": 0,
      "failed": 0
    },
    "status": "queued",
    "status_url": "http://server/start/job_id_uuid",
    "results_url": "http://server/start/job_id_uuid/results"
  }
  ```
* **Common Errors**:
  - `400 Bad Request`: Missing files, mismatched deployment name, or empty manifest.
  - `429 Too Many Requests`: Instruction quota exceeded.
  - `503 Service Unavailable`: No active worker nodes are connected to Redis.

### Get Job Status
* **Method & Path**: `GET /start/<job_id>`
* **Auth**: User API Key
* **Headers**: `X-Job-Token: <JOB_TOKEN>` or query parameter `?token=<JOB_TOKEN>`
* **Success Response (200 OK)**:
  ```json
  {
    "job_id": "job_id_uuid",
    "pid": "deployment_pid",
    "name": "project_name",
    "status": "running",
    "done": false,
    "counts": { "queued": 0, "running": 1, "completed": 1, "failed": 0 },
    "tasks": [
      {
        "tid": "task_uuid_1",
        "status": "completed",
        "assigned_node": "node_xyz"
      }
    ],
    "metadata": {},
    "created_at": "timestamp",
    "updated_at": "timestamp"
  }
  ```

### Get Job Results
* **Method & Path**: `GET /start/<job_id>/results`
* **Auth**: User API Key
* **Headers**: `X-Job-Token: <JOB_TOKEN>` or query parameter `?token=<JOB_TOKEN>`
* **Success Response (200 OK - Job Finished)**:
  ```json
  {
    "job_id": "job_id_uuid",
    "pid": "deployment_pid",
    "name": "project_name",
    "status": "completed",
    "done": true,
    "counts": { "queued": 0, "running": 0, "completed": 2, "failed": 0 },
    "results": [
      {
        "tid": "task_uuid_1",
        "status": "completed",
        "result_b64": "base64_encoded_result_bytes"
      }
    ]
  }
  ```
* **Pending Response (202 Accepted - Job Still Running)**:
  Returns status and task counts, but `done` is set to `false`.

---

## Worker Node Endpoints

### Register Node
* **Method & Path**: `POST /nodes/register`
* **Auth**: One of the following sent in `Authorization: Bearer <TOKEN>` header:
  - User API Key (claims node ownership).
  - Node registration token (for headless nodes).
* **Request Body (JSON)**:
  ```json
  {
    "supports_wasm": true,
    "rsa_public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
  }
  ```
* **Success Response (201 Created)**:
  ```json
  {
    "status": "Registered",
    "node_id": "node_xyz123",
    "node_token": "secret_node_token_here"
  }
  ```
* **Common Errors**:
  - `401 Unauthorized`: Missing registration token.
  - `403 Forbidden`: Invalid registration token.

### Node Health Check
* **Method & Path**: `POST /nodes/health`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Request Body (JSON)**:
  ```json
  {
    "latency": 15,
    "download": 100.5,
    "upload": 20.3
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "status": "Alive"
  }
  ```

### Claim Task
* **Method & Path**: `POST /nodes/tasks/claim`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Success Response (200 OK - Task Found)**:
  ```json
  {
    "tid": "task_uuid_1",
    "job_id": "job_uuid",
    "task_name": "process_data",
    "filename": "task.wasm",
    "runtime": "wasm",
    "claim_token": "claim_token_secret",
    "download_url": "http://server/nodes/tasks/task_uuid_1/download/token",
    "timeout_ms": 5000
  }
  ```
* **Idle Response (204 No Content)**:
  Returned when no tasks are currently waiting for execution.

### Download Task File
* **Method & Path**: `GET /nodes/tasks/<tid>/download/<download_token>`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Success Response (200 OK)**:
  Returns the binary `.wasm` or pickle file download.
  - **Headers**:
    - `X-Task-Dek-Encrypted`: Base64 encrypted data encryption key.
    - `X-Task-IV`: Base64 initialization vector.

### Submit Task Result
* **Method & Path**: `POST /nodes/tasks/<tid>/result`
* **Auth**: Node Token
* **Headers**:
  - `X-Node-Id: <NODE_ID>`
  - `X-Task-Claim: <CLAIM_TOKEN>`
  - `X-Execution-Receipt: <JSON_RECEIPT_B64>` (For WASM tasks)
* **Request Body**:
  - If successful: Send binary results raw in the body with `application/octet-stream` mime-type.
  - If failed: Send JSON body `{"error": "error message"}`.
* **Success Response (200 OK)**:
  ```json
  {
    "status": "completed",
    "job_status": "running",
    "counts": { "queued": 0, "running": 1, "completed": 1, "failed": 0 }
  }
  ```

---

## Web Application Hosting (Serve) Endpoints

These routes require a **User API Key** (for developers) or **Node Token** (for workers).

### Deploy Web App
* **Method & Path**: `POST /serve/deploy`
* **Auth**: User API Key
* **Request Body (Multipart Form-Data)**:
  - `bundle` (Required): `.tar` archive of the app folder.
  - `start_command` (Required): Command string to launch the app (e.g. `python app.py`).
  - `replicas` (Optional): Number of workers to run the app on.
  - `name` (Optional): App name.
* **Success Response (201 Created)**:
  ```json
  {
    "pid": "serve_xyz123",
    "url": "/app/serve_xyz123/"
  }
  ```

### List App Deployments
* **Method & Path**: `GET /serve`
* **Auth**: User API Key
* **Success Response (200 OK)**:
  ```json
  {
    "deployments": [
      {
        "pid": "serve_xyz123",
        "name": "my_app",
        "status": "running",
        "replicas": 2,
        "serving_nodes": ["node_1", "node_2"],
        "url": "/app/serve_xyz123/"
      }
    ]
  }
  ```

### Stop App Deployment
* **Method & Path**: `DELETE /serve/<pid>`
* **Auth**: User API Key
* **Success Response (200 OK)**:
  ```json
  {
    "ok": true,
    "pid": "serve_xyz123"
  }
  ```

### Claim Web App Assignment (Node)
* **Method & Path**: `POST /nodes/serve/claim`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Success Response (200 OK - Assignment Found)**:
  ```json
  {
    "pid": "serve_xyz123",
    "start_command": ["python", "app.py"],
    "replicas": 2
  }
  ```
* **Idle Response (204 No Content)**:
  No assignments available.

### Download App Bundle (Node)
* **Method & Path**: `GET /nodes/serve/<pid>/bundle`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Success Response (200 OK)**:
  Returns the application `.tar` bundle file.

### Poll Web Requests (Node)
* **Method & Path**: `POST /nodes/serve/next`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Request Body (JSON)**:
  ```json
  {
    "pids": ["serve_xyz123"]
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "request": {
      "req_id": "request_uuid",
      "method": "GET",
      "path": "/index",
      "headers": [["Host", "server_ip"]],
      "body_b64": ""
    },
    "stop": []
  }
  ```

### Submit Web App Response (Node)
* **Method & Path**: `POST /nodes/serve/response/<req_id>`
* **Auth**: Node Token
* **Headers**: `X-Node-Id: <NODE_ID>`
* **Request Body (JSON)**:
  ```json
  {
    "status": 200,
    "headers": [["Content-Type", "text/html"]],
    "body_b64": "Ym9keSBjb250ZW50"
  }
  ```
* **Success Response (200 OK)**:
  ```json
  {
    "ok": true
  }
  ```

---

## Load Balancer (Public Gateway)

### Forward Public Web Traffic
* **Method & Path**: `* /app/<pid>/` and `* /app/<pid>/<path>` (Supports GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
* **Auth**: None (Public)
* **Success Response**: Matches the response headers and body returned by the node hosting the application.
