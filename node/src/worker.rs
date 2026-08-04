use std::time::Duration;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use rsa::RsaPrivateKey;
use serde::Deserialize;

use crate::config::NodeConfig;
use crate::crypto;
use crate::executor;

// ── Server DTOs ─────────────────────────────────────────────────────────────

#[derive(Deserialize, Debug)]
struct ClaimResponse {
    tid: String,
    download_url: String,
    claim_token: String,
    runtime: String,
    #[serde(default)]
    task_name: Option<String>,
    #[serde(default)]
    timeout_ms: Option<u64>,
}

// ── Constants ───────────────────────────────────────────────────────────────

const MIN_POLL_INTERVAL: Duration = Duration::from_secs(1);
const MAX_POLL_INTERVAL: Duration = Duration::from_secs(10);
const HTTP_TIMEOUT: Duration = Duration::from_secs(30);

// ── Public entry ────────────────────────────────────────────────────────────

/// Run the task-claim / execute / report loop until the process is
/// terminated.
pub async fn task_loop(config: &NodeConfig, private_key: &RsaPrivateKey) {
    let client = reqwest::Client::builder()
        .timeout(HTTP_TIMEOUT)
        .build()
        .expect("failed to build HTTP client for task loop");

    let claim_url = format!(
        "{}/nodes/tasks/claim",
        config.server_url.trim_end_matches('/')
    );

    let mut backoff = MIN_POLL_INTERVAL;

    loop {
        // ── 1. Claim a task ─────────────────────────────────────────────
        let claim_result = client
            .post(&claim_url)
            .header("X-Node-Id", &config.node_id)
            .header("Authorization", format!("Bearer {}", config.node_token))
            .send()
            .await;

        let resp = match claim_result {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[worker] claim request failed: {e}");
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(MAX_POLL_INTERVAL);
                continue;
            }
        };

        let status = resp.status();

        // 204 No Content — nothing to do right now.
        if status == reqwest::StatusCode::NO_CONTENT {
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(MAX_POLL_INTERVAL);
            continue;
        }

        if !status.is_success() {
            eprintln!("[worker] unexpected status from claim: {status}");
            tokio::time::sleep(backoff).await;
            backoff = (backoff * 2).min(MAX_POLL_INTERVAL);
            continue;
        }

        // Reset back-off on a successful 200.
        backoff = MIN_POLL_INTERVAL;

        let task: ClaimResponse = match resp.json().await {
            Ok(t) => t,
            Err(e) => {
                eprintln!("[worker] failed to parse claim response: {e}");
                continue;
            }
        };

        eprintln!(
            "[worker] claimed task {} (runtime={}, name={:?})",
            task.tid, task.runtime, task.task_name
        );

        // ── 2. Validate runtime ─────────────────────────────────────────
        if task.runtime != "wasm" {
            eprintln!("[worker] unsupported runtime '{}' — skipping", task.runtime);
            report_failure(
                &client,
                config,
                &task.tid,
                &task.claim_token,
                &format!("unsupported runtime: {}", task.runtime),
            )
            .await;
            continue;
        }

        // ── 3. Download blob ────────────────────────────────────────────
        let blob_result = client
            .get(&task.download_url)
            .header("X-Node-Id", &config.node_id)
            .header("Authorization", format!("Bearer {}", config.node_token))
            .send()
            .await;

        let blob_resp = match blob_result {
            Ok(r) if r.status().is_success() => r,
            Ok(r) => {
                let s = r.status();
                eprintln!("[worker] blob download failed with {s}");
                report_failure(&client, config, &task.tid, &task.claim_token, "blob download failed").await;
                continue;
            }
            Err(e) => {
                eprintln!("[worker] blob download error: {e}");
                report_failure(&client, config, &task.tid, &task.claim_token, "blob download error").await;
                continue;
            }
        };

        // Extract optional encryption headers.
        let encrypted_dek = blob_resp
            .headers()
            .get("X-Task-Dek-Encrypted")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());
        let iv = blob_resp
            .headers()
            .get("X-Task-IV")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.to_string());

        let raw_bytes = match blob_resp.bytes().await {
            Ok(b) => b.to_vec(),
            Err(e) => {
                eprintln!("[worker] failed to read blob body: {e}");
                report_failure(&client, config, &task.tid, &task.claim_token, "failed to read blob").await;
                continue;
            }
        };

        // ── 4. Decrypt (if encrypted) ───────────────────────────────────
        let wasm_bytes = if let (Some(dek_b64), Some(iv_b64)) = (&encrypted_dek, &iv) {
            match crypto::decrypt_dek(private_key, dek_b64) {
                Ok(dek) => match crypto::decrypt_blob(&dek, iv_b64, &raw_bytes) {
                    Ok(plain) => plain,
                    Err(e) => {
                        eprintln!("[worker] blob decryption failed: {e}");
                        report_failure(&client, config, &task.tid, &task.claim_token, &format!("decryption error: {e}")).await;
                        continue;
                    }
                },
                Err(e) => {
                    eprintln!("[worker] DEK decryption failed: {e}");
                    report_failure(&client, config, &task.tid, &task.claim_token, &format!("DEK decryption error: {e}")).await;
                    continue;
                }
            }
        } else {
            // Unencrypted (backward-compat).
            raw_bytes
        };

        // ── 5. Execute ──────────────────────────────────────────────────
        let timeout_ms = task.timeout_ms;
        let exec_result = tokio::task::spawn_blocking(move || {
            executor::execute_wasm(&wasm_bytes, timeout_ms).map_err(|e| e.to_string())
        })
        .await
        .unwrap_or_else(|e| Err(format!("spawn_blocking error: {}", e)));

        match exec_result {
            Ok(result) => {
                report_success(
                    &client,
                    config,
                    private_key,
                    &task.tid,
                    &task.claim_token,
                    &result,
                )
                .await;
            }
            Err(e) => {
                eprintln!("[worker] execution error: {e}");
                report_failure(
                    &client,
                    config,
                    &task.tid,
                    &task.claim_token,
                    &format!("{e}"),
                )
                .await;
            }
        }

        // Immediately poll for next task (no sleep).
    }
}

// ── Result reporting helpers ────────────────────────────────────────────────

async fn report_success(
    client: &reqwest::Client,
    config: &NodeConfig,
    private_key: &RsaPrivateKey,
    tid: &str,
    claim_token: &str,
    result: &executor::ExecutionResult,
) {
    let output_hash = crypto::sha256_hex(&result.output);

    // Canonical message for the execution receipt.
    let canonical = format!(
        "{}|{}|{}|{}",
        tid, result.instruction_count, result.memory_hash, output_hash
    );

    let signature = match crypto::sign_receipt(private_key, canonical.as_bytes()) {
        Ok(sig) => sig,
        Err(e) => {
            eprintln!("[worker] failed to sign receipt: {e}");
            return;
        }
    };

    let receipt = serde_json::json!({
        "tid": tid,
        "instruction_count": result.instruction_count,
        "memory_hash": result.memory_hash,
        "output_hash": output_hash,
        "signature": signature,
    });
    let receipt_b64 = B64.encode(receipt.to_string().as_bytes());

    let url = format!(
        "{}/nodes/tasks/{}/result",
        config.server_url.trim_end_matches('/'),
        tid
    );

    let resp = client
        .post(&url)
        .header("X-Node-Id", &config.node_id)
        .header("Authorization", format!("Bearer {}", config.node_token))
        .header("X-Task-Claim", claim_token)
        .header("X-Execution-Receipt", &receipt_b64)
        .header("Content-Type", "application/octet-stream")
        .body(result.output.clone())
        .send()
        .await;

    match resp {
        Ok(r) if r.status().is_success() => {
            eprintln!("[worker] result submitted for task {tid}");
        }
        Ok(r) => {
            let s = r.status();
            let body = r.text().await.unwrap_or_default();
            eprintln!("[worker] result submission got {s}: {body}");
        }
        Err(e) => {
            eprintln!("[worker] result submission failed: {e}");
        }
    }
}

async fn report_failure(
    client: &reqwest::Client,
    config: &NodeConfig,
    tid: &str,
    claim_token: &str,
    error_msg: &str,
) {
    let url = format!(
        "{}/nodes/tasks/{}/result",
        config.server_url.trim_end_matches('/'),
        tid
    );

    let body = serde_json::json!({ "error": error_msg });

    let resp = client
        .post(&url)
        .header("X-Node-Id", &config.node_id)
        .header("Authorization", format!("Bearer {}", config.node_token))
        .header("X-Task-Claim", claim_token)
        .header("Content-Type", "application/json")
        .json(&body)
        .send()
        .await;

    match resp {
        Ok(r) if r.status().is_success() => {
            eprintln!("[worker] failure reported for task {tid}");
        }
        Ok(r) => {
            eprintln!(
                "[worker] failure report got {}: {}",
                r.status(),
                r.text().await.unwrap_or_default()
            );
        }
        Err(e) => {
            eprintln!("[worker] could not report failure for {tid}: {e}");
        }
    }
}
