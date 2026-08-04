//! Hosting a user's web app on this node.
//!
//! A serve deployment is a real web app (Flask, FastAPI, a plain stdlib server,
//! ...). We run it inside the hardened sandbox with **no network at all**
//! (`--unshare-net`), so it can't phone home or be reached from the outside. It
//! talks to us over a **unix domain socket** in its own working directory
//! instead: the app binds `$TANDEM_SERVE_SOCKET`, and the node proxies HTTP to
//! that socket. The node is the app's only door to the world.
//!
//! This module owns two things: launching the app in the sandbox and waiting for
//! it to come up (`ServedApp`), and forwarding one HTTP request to it over the
//! socket (`proxy_request`). The loop that pulls requests from the server and
//! feeds them here lives in the worker wiring.

// The serve request/claim loop is still being wired into main, so a couple of
// these are only used by tests for now.
#![allow(dead_code)]

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use base64::Engine;

use crate::config::NodeConfig;
use crate::sandbox::{spawn_sandboxed, SandboxLimits, SandboxSpec};

type BoxError = Box<dyn Error + Send + Sync>;

/// The filename of the unix socket the app binds, inside its working directory.
const SOCKET_NAME: &str = "tandem-serve.sock";

/// One HTTP response proxied back from a hosted app.
#[derive(Debug, Clone)]
pub struct HttpResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// A running hosted app: the sandboxed child plus where its socket lives.
pub struct ServedApp {
    child: Child,
    socket_path: PathBuf,
}

impl ServedApp {
    /// Where the app's unix socket lives on the node's filesystem.
    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    /// Stop the app. Best-effort: ask it to die, then don't block forever.
    pub fn shutdown(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_file(&self.socket_path);
    }
}

/// Launch a web app in the sandbox and wait for its socket to come up.
///
/// * `work_dir` already contains the unpacked app (its code is at the top).
/// * `start_command` is what the user told us to run, e.g. `["python3", "app.py"]`.
/// * `limits` are the resource ceilings (the account's RAM cap, etc.).
/// Fork the app in the sandbox WITHOUT waiting for its socket to come up.
///
/// Important subtlety: bwrap's `--die-with-parent` ties the app's lifetime to
/// the *thread* that forked it (Linux `PR_SET_PDEATHSIG` is thread-scoped, not
/// process-scoped). So this must be called from a long-lived runtime thread --
/// never a transient one like a `spawn_blocking` worker that tokio may reap,
/// which would kill the app out from under us seconds after it started.
pub fn spawn_app(
    work_dir: &Path,
    start_command: &[String],
    node_id: &str,
    limits: SandboxLimits,
) -> io::Result<ServedApp> {
    let socket_path = work_dir.join(SOCKET_NAME);
    // Clear any stale socket from a previous run so the app can bind cleanly.
    let _ = std::fs::remove_file(&socket_path);

    let spec = SandboxSpec {
        work_dir: work_dir.to_path_buf(),
        command: start_command.to_vec(),
        env: vec![
            ("PATH".to_string(), "/usr/bin:/bin".to_string()),
            ("HOME".to_string(), "/app".to_string()),
            // The app binds this socket; inside the sandbox the work dir is /app.
            (
                "TANDEM_SERVE_SOCKET".to_string(),
                format!("/app/{SOCKET_NAME}"),
            ),
            // So the app can tell which node it's running on (handy for showing
            // that the load balancer really is spreading traffic).
            ("TANDEM_NODE_ID".to_string(), node_id.to_string()),
        ],
        allow_network: false,
        limits,
    };

    let child = spawn_sandboxed(&spec)?;
    Ok(ServedApp { child, socket_path })
}

/// Poll for the app's socket without blocking a runtime thread. The per-attempt
/// connect is a near-instant local syscall; the wait between attempts yields
/// back to the runtime.
async fn wait_ready(socket_path: &Path, timeout: Duration) -> io::Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if socket_path.exists() {
            if let Ok(stream) = UnixStream::connect(socket_path) {
                drop(stream);
                return Ok(());
            }
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "hosted app did not start listening on its socket in time",
            ));
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Forward one HTTP request to the app over its unix socket and read the reply.
///
/// We speak plain HTTP/1.1 with `Connection: close`, which keeps the proxy
/// simple: send the request, read until the app closes the socket, done. That's
/// plenty for request/response web apps.
pub fn proxy_request(
    socket_path: &Path,
    method: &str,
    path: &str,
    headers: &[(String, String)],
    body: &[u8],
) -> io::Result<HttpResponse> {
    let mut stream = UnixStream::connect(socket_path)?;
    stream.set_read_timeout(Some(Duration::from_secs(60)))?;

    let mut request = Vec::new();
    request.extend_from_slice(format!("{method} {path} HTTP/1.1\r\n").as_bytes());
    request.extend_from_slice(b"Host: tandem\r\n");
    request.extend_from_slice(b"Connection: close\r\n");
    for (name, value) in headers {
        let lowered = name.to_ascii_lowercase();
        // We set these ourselves, so skip any the caller passed.
        if lowered == "host" || lowered == "connection" || lowered == "content-length" {
            continue;
        }
        request.extend_from_slice(format!("{name}: {value}\r\n").as_bytes());
    }
    request.extend_from_slice(format!("Content-Length: {}\r\n", body.len()).as_bytes());
    request.extend_from_slice(b"\r\n");
    request.extend_from_slice(body);

    stream.write_all(&request)?;
    stream.flush()?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw)?;
    parse_http_response(&raw)
}

/// Split a raw HTTP/1.1 response into status, headers, and body.
fn parse_http_response(raw: &[u8]) -> io::Result<HttpResponse> {
    // Find the blank line that separates headers from the body.
    let split_at = raw.windows(4).position(|w| w == b"\r\n\r\n").ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidData, "malformed HTTP response from app")
    })?;
    let head = &raw[..split_at];
    let body = raw[split_at + 4..].to_vec();

    let head_text = String::from_utf8_lossy(head);
    let mut lines = head_text.split("\r\n");

    let status_line = lines.next().unwrap_or("");
    let status = parse_status_code(status_line)?;

    let mut headers = Vec::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            headers.push((name.trim().to_string(), value.trim().to_string()));
        }
    }

    Ok(HttpResponse {
        status,
        headers,
        body,
    })
}

/// Pull the numeric status code out of a line like `HTTP/1.1 200 OK`.
fn parse_status_code(status_line: &str) -> io::Result<u16> {
    let mut parts = status_line.split_whitespace();
    let _version = parts.next();
    let code = parts.next().unwrap_or("");
    code.parse::<u16>()
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "bad HTTP status line from app"))
}

// ---------------------------------------------------------------------------
// Coordination with the server: claim a deployment, then serve its requests.
// ---------------------------------------------------------------------------

/// Where a deployment's unpacked app lives on this node.
fn serve_work_dir(pid: &str) -> PathBuf {
    std::env::temp_dir().join("tandem-serve").join(pid)
}

/// One request the load balancer handed us to run against a hosted app.
struct PendingRequest {
    req_id: String,
    pid: String,
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

/// Add the node's auth headers to an outgoing request.
fn with_node_auth(builder: reqwest::RequestBuilder, cfg: &NodeConfig) -> reqwest::RequestBuilder {
    builder
        .header("X-Node-Id", &cfg.node_id)
        .header("Authorization", format!("Bearer {}", cfg.node_token))
}

/// The node's serve side: keep asking the server for a deployment to host, and
/// once we're hosting one, keep pulling its requests and proxying them.
///
/// This runs forever alongside the compute task loop. It only ever talks *out*
/// to the server, same as everything else the node does.
/// How many times we try to start a deployment before giving up on it and
/// telling the server it's broken, so it stops being re-assigned forever.
const MAX_START_ATTEMPTS: u32 = 3;

/// What `/nodes/serve/next` hands back: maybe a request to proxy, plus any pids
/// we should stop hosting (the deployment was removed or marked failed).
struct NextOutcome {
    request: Option<PendingRequest>,
    stop: Vec<String>,
}

/// Shared handle to the apps this node is currently hosting.
type AppMap = Arc<Mutex<HashMap<String, ServedApp>>>;

pub async fn serve_loop(cfg: NodeConfig) {
    let client = reqwest::Client::new();
    let apps: AppMap = Arc::new(Mutex::new(HashMap::new()));
    // pids we're mid-setup on, and pids we've permanently given up starting, so
    // a re-assignment of the same pid doesn't kick off a duplicate/pointless try.
    let starting: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));
    let given_up: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));

    loop {
        // 1. Pick up a new assignment. Starting it (download + wait for its
        //    socket, up to ~15s) runs in its OWN task, so a slow or failing
        //    start never blocks the serving/heartbeat step below -- that was the
        //    old bug where one broken deploy starved every healthy one.
        match try_claim(&client, &cfg).await {
            Ok(Some((pid, start_command))) => {
                let fresh = {
                    let hosting = apps.lock().unwrap();
                    let in_flight = starting.lock().unwrap();
                    let abandoned = given_up.lock().unwrap();
                    !hosting.contains_key(&pid)
                        && !in_flight.contains(&pid)
                        && !abandoned.contains(&pid)
                };
                if fresh {
                    eprintln!("[serve] assigned deployment {pid}");
                    starting.lock().unwrap().insert(pid.clone());
                    let client = client.clone();
                    let cfg = cfg.clone();
                    let apps = Arc::clone(&apps);
                    let starting = Arc::clone(&starting);
                    let given_up = Arc::clone(&given_up);
                    tokio::spawn(async move {
                        start_with_retries(&client, &cfg, &pid, &start_command, &apps, &given_up)
                            .await;
                        starting.lock().unwrap().remove(&pid);
                    });
                }
            }
            Ok(None) => {}
            Err(err) => eprintln!("[serve] claim error: {err}"),
        }

        // 2. Serve one request across everything we host (this also heartbeats
        //    each app so the load balancer keeps it "healthy serving").
        let pids: Vec<String> = { apps.lock().unwrap().keys().cloned().collect() };
        if pids.is_empty() {
            tokio::time::sleep(Duration::from_millis(500)).await;
            continue;
        }

        match next_request(&client, &cfg, &pids).await {
            Ok(outcome) => {
                for pid in outcome.stop {
                    let removed = apps.lock().unwrap().remove(&pid);
                    if let Some(mut app) = removed {
                        app.shutdown();
                        eprintln!("[serve] stopped hosting {pid} (removed on the server)");
                    }
                }
                if let Some(req) = outcome.request {
                    handle_request(&client, &cfg, &apps, req).await;
                }
            }
            Err(err) => {
                eprintln!("[serve] next-request error: {err}");
                tokio::time::sleep(Duration::from_millis(500)).await;
            }
        }
    }
}

/// Start a deployment, retrying a few times. If it never comes up, tell the
/// server it's broken and stop trying it, so it isn't re-assigned forever.
async fn start_with_retries(
    client: &reqwest::Client,
    cfg: &NodeConfig,
    pid: &str,
    start_command: &[String],
    apps: &AppMap,
    given_up: &Arc<Mutex<HashSet<String>>>,
) {
    for attempt in 1..=MAX_START_ATTEMPTS {
        match setup_app(client, cfg, pid, start_command).await {
            Ok(app) => {
                apps.lock().unwrap().insert(pid.to_string(), app);
                eprintln!("[serve] {pid} is up and serving");
                return;
            }
            Err(err) => {
                eprintln!(
                    "[serve] start attempt {attempt}/{MAX_START_ATTEMPTS} for {pid} failed: {err}"
                );
                if attempt < MAX_START_ATTEMPTS {
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
    }
    given_up.lock().unwrap().insert(pid.to_string());
    eprintln!("[serve] giving up on {pid} after {MAX_START_ATTEMPTS} attempts; reporting it failed");
    if let Err(err) = report_failed(client, cfg, pid).await {
        eprintln!("[serve] could not report {pid} as failed: {err}");
    }
}

/// Tell the server a deployment couldn't be started, so it stops assigning it.
async fn report_failed(client: &reqwest::Client, cfg: &NodeConfig, pid: &str) -> Result<(), BoxError> {
    let url = format!("{}/nodes/serve/{}/failed", cfg.server_url, pid);
    let response = with_node_auth(client.post(&url), cfg).send().await?;
    if !response.status().is_success() {
        return Err(format!("failed-report returned {}", response.status()).into());
    }
    Ok(())
}

/// Ask the server for a serve assignment. Returns `(pid, start_command)`.
async fn try_claim(
    client: &reqwest::Client,
    cfg: &NodeConfig,
) -> Result<Option<(String, Vec<String>)>, BoxError> {
    let url = format!("{}/nodes/serve/claim", cfg.server_url);
    let response = with_node_auth(client.post(&url), cfg).send().await?;

    if response.status() == reqwest::StatusCode::NO_CONTENT {
        return Ok(None);
    }
    if !response.status().is_success() {
        return Err(format!("claim returned {}", response.status()).into());
    }

    let value: serde_json::Value = response.json().await?;
    let pid = value["pid"].as_str().ok_or("assignment missing pid")?.to_string();
    let start_command = value["start_command"]
        .as_array()
        .ok_or("assignment missing start_command")?
        .iter()
        .map(|item| item.as_str().unwrap_or("").to_string())
        .collect();
    Ok(Some((pid, start_command)))
}

/// Download the app bundle, unpack it, and launch it in the sandbox.
async fn setup_app(
    client: &reqwest::Client,
    cfg: &NodeConfig,
    pid: &str,
    start_command: &[String],
) -> Result<ServedApp, BoxError> {
    let url = format!("{}/nodes/serve/{}/bundle", cfg.server_url, pid);
    let response = with_node_auth(client.get(&url), cfg).send().await?;
    if !response.status().is_success() {
        return Err(format!("bundle download returned {}", response.status()).into());
    }
    let bytes = response.bytes().await?;

    let work_dir = serve_work_dir(pid);

    // Unpack the bundle off the runtime. `tar` is a short-lived child that exits
    // on its own, so there's no --die-with-parent concern in doing this on a
    // transient blocking thread (unlike forking the app itself, below).
    {
        let work_dir = work_dir.clone();
        tokio::task::spawn_blocking(move || -> io::Result<()> {
            let _ = fs::remove_dir_all(&work_dir);
            fs::create_dir_all(&work_dir)?;

            let tar_path = work_dir.join("bundle.tar");
            fs::write(&tar_path, &bytes)?;
            let status = Command::new("tar")
                .arg("-xf")
                .arg(&tar_path)
                .arg("-C")
                .arg(&work_dir)
                .status()?;
            if !status.success() {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    "could not unpack the app bundle",
                ));
            }
            let _ = fs::remove_file(&tar_path);
            Ok(())
        })
        .await??;
    }

    // Fork the app on THIS runtime thread (a long-lived tokio worker) so
    // bwrap's thread-scoped --die-with-parent ties it to the node's lifetime,
    // then wait for its socket asynchronously.
    let mut app = spawn_app(&work_dir, start_command, &cfg.node_id, SandboxLimits::default())?;
    if let Err(err) = wait_ready(app.socket_path(), Duration::from_secs(15)).await {
        app.shutdown();
        return Err(err.into());
    }
    Ok(app)
}

/// Long-poll the server for the next request across the deployments we host.
async fn next_request(
    client: &reqwest::Client,
    cfg: &NodeConfig,
    pids: &[String],
) -> Result<NextOutcome, BoxError> {
    let url = format!("{}/nodes/serve/next", cfg.server_url);
    let response = with_node_auth(client.post(&url), cfg)
        .json(&serde_json::json!({ "pids": pids }))
        .send()
        .await?;

    // 204 = nothing to do right now (no request, nothing to stop).
    if response.status() == reqwest::StatusCode::NO_CONTENT {
        return Ok(NextOutcome { request: None, stop: Vec::new() });
    }
    if !response.status().is_success() {
        return Err(format!("next-request returned {}", response.status()).into());
    }

    let value: serde_json::Value = response.json().await?;

    let stop: Vec<String> = value["stop"]
        .as_array()
        .map(|items| items.iter().filter_map(|v| v.as_str().map(String::from)).collect())
        .unwrap_or_default();

    // The request lives under "request"; treat a missing/empty req_id as "none".
    let req_val = &value["request"];
    let request = match req_val.get("req_id").and_then(|v| v.as_str()) {
        Some(req_id) if !req_id.is_empty() => {
            let headers: Vec<(String, String)> =
                serde_json::from_str(req_val["headers"].as_str().unwrap_or("[]")).unwrap_or_default();
            let body = base64::engine::general_purpose::STANDARD
                .decode(req_val["body_b64"].as_str().unwrap_or(""))
                .unwrap_or_default();
            Some(PendingRequest {
                req_id: req_id.to_string(),
                pid: req_val["pid"].as_str().unwrap_or("").to_string(),
                method: req_val["method"].as_str().unwrap_or("GET").to_string(),
                path: req_val["path"].as_str().unwrap_or("/").to_string(),
                headers,
                body,
            })
        }
        _ => None,
    };

    Ok(NextOutcome { request, stop })
}

/// Proxy one request to its app and send the response back to the server.
async fn handle_request(
    client: &reqwest::Client,
    cfg: &NodeConfig,
    apps: &AppMap,
    req: PendingRequest,
) {
    // Grab just the socket path under the lock, then release it before the
    // blocking proxy call below.
    let socket = {
        let hosting = apps.lock().unwrap();
        match hosting.get(&req.pid) {
            Some(app) => app.socket_path().to_path_buf(),
            None => return,
        }
    };

    // The proxy call is blocking (a plain unix socket), so run it off the async
    // runtime's threads.
    let proxied = tokio::task::spawn_blocking(move || {
        proxy_request(&socket, &req.method, &req.path, &req.headers, &req.body)
    })
    .await;

    let response = match proxied {
        Ok(Ok(response)) => response,
        _ => HttpResponse {
            status: 502,
            headers: Vec::new(),
            body: b"the app could not handle the request".to_vec(),
        },
    };

    if let Err(err) = send_response(client, cfg, &req.req_id, &response).await {
        eprintln!("[serve] could not return the response: {err}");
    }
}

/// Hand one app response back to the server for the load balancer to return.
async fn send_response(
    client: &reqwest::Client,
    cfg: &NodeConfig,
    req_id: &str,
    response: &HttpResponse,
) -> Result<(), BoxError> {
    let body_b64 = base64::engine::general_purpose::STANDARD.encode(&response.body);
    let payload = serde_json::json!({
        "status": response.status,
        "headers": response.headers,
        "body_b64": body_b64,
    });

    let url = format!("{}/nodes/serve/response/{}", cfg.server_url, req_id);
    let result = with_node_auth(client.post(&url), cfg)
        .json(&payload)
        .send()
        .await?;
    if !result.status().is_success() {
        return Err(format!("submitting response returned {}", result.status()).into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sandbox::bwrap_available;

    // A tiny stdlib-only web app that binds the unix socket Tandem hands it and
    // answers every GET with a fixed body. No third-party deps, so it runs in the
    // no-network sandbox as-is.
    const SAMPLE_APP: &str = r#"
import os, socketserver, http.server

SOCK = os.environ["TANDEM_SERVE_SOCKET"]

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"hello-from-serve"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

class UnixHTTPServer(socketserver.UnixStreamServer):
    def get_request(self):
        conn, _ = self.socket.accept()
        return conn, ("local", 0)

try:
    os.remove(SOCK)
except FileNotFoundError:
    pass

UnixHTTPServer(SOCK, Handler).serve_forever()
"#;

    #[tokio::test]
    async fn hosts_an_app_in_the_sandbox_and_proxies_a_request() {
        if !bwrap_available() {
            return; // sandbox not available on this machine; skip
        }

        let work = std::env::temp_dir().join("tandem_serve_test_app");
        let _ = std::fs::create_dir_all(&work);
        std::fs::write(work.join("app.py"), SAMPLE_APP).unwrap();

        let start = vec!["python3".to_string(), "app.py".to_string()];
        let mut app = match spawn_app(&work, &start, "test-node", SandboxLimits::default()) {
            Ok(app) => app,
            Err(_) => return, // no python3 in the sandbox on this machine; skip
        };
        if wait_ready(app.socket_path(), Duration::from_secs(15)).await.is_err() {
            app.shutdown();
            return; // app didn't come up in time; skip
        }

        let response = proxy_request(app.socket_path(), "GET", "/", &[], b"")
            .expect("proxy request should succeed");
        app.shutdown();

        assert_eq!(response.status, 200);
        assert_eq!(response.body, b"hello-from-serve");
    }
}
