use std::env;
use std::fs;
use std::thread;

use rand::rngs::OsRng;
use rsa::pkcs8::{EncodePrivateKey, EncodePublicKey, LineEnding};
use rsa::RsaPrivateKey;
use serde::{Deserialize, Serialize};
use sysinfo::{CpuRefreshKind, RefreshKind, System};

const RSA_KEY_BITS: usize = 4096;

#[derive(Serialize)]
struct RegisterRequest {
    supports_wasm: bool,
    rsa_public_key_pem: String,
    cpu_cores: usize,
    cpu_name: String,
    cpu_mhz: u64,
    memory_mb: u64,
}

/// CPU core count for spec-aware scheduling, or 1 if the OS won't say.
fn cpu_cores() -> usize {
    thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1)
}

/// The CPU's model name and clock speed (MHz). Every core reports the same
/// model on real hardware, so the first one is enough.
fn cpu_info() -> (String, u64) {
    let mut sys = System::new_with_specifics(
        RefreshKind::nothing().with_cpu(CpuRefreshKind::everything()),
    );
    sys.refresh_cpu_all();

    match sys.cpus().first() {
        // The brand string comes straight from CPUID and is often space-padded.
        Some(cpu) => (cpu.brand().trim().to_string(), cpu.frequency()),
        None => ("unknown".to_string(), 0),
    }
}

/// Total system memory in megabytes. Refreshes only memory, skipping the
/// heavier `System::new_all()` process and disk scan.
fn memory_mb() -> u64 {
    let mut sys = System::new();
    sys.refresh_memory();
    sys.total_memory() / 1024 / 1024
}

#[derive(Deserialize)]
struct RegisterResponse {
    node_id: String,
    node_token: String,
}

/// The bearer token to send when registering, if we have one. A logged-in
/// user's auth token wins; otherwise we fall back to the shared registration
/// token. Empty counts as unset.
fn registration_auth() -> Option<String> {
    for var in ["TANDEM_NODE_AUTH_TOKEN", "TANDEM_NODE_REGISTRATION_TOKEN"] {
        if let Ok(value) = env::var(var)
            && !value.is_empty()
        {
            return Some(value);
        }
    }
    None
}

/// Generate an RSA-4096 keypair, persist the private key, and register with the
/// Tandem server.  Returns `(node_id, node_token)`.
pub async fn register_node(
    server_url: &str,
    private_key_path: &str,
) -> Result<(String, String), Box<dyn std::error::Error>> {
    eprintln!("[registration] generating RSA-4096 keypair — this may take a moment…");

    let private_key = RsaPrivateKey::new(&mut OsRng, RSA_KEY_BITS)?;

    let pem = private_key.to_pkcs8_pem(LineEnding::LF)?;
    fs::write(private_key_path, pem.as_bytes())?;
    eprintln!("[registration] private key saved to {private_key_path}");

    let pub_pem = private_key
        .to_public_key()
        .to_public_key_pem(LineEnding::LF)?;

    let (cpu_name, cpu_mhz) = cpu_info();
    let body = RegisterRequest {
        supports_wasm: true,
        rsa_public_key_pem: pub_pem,
        cpu_cores: cpu_cores(),
        cpu_name,
        cpu_mhz,
        memory_mb: memory_mb(),
    };

    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    let url = format!("{}/nodes/register", server_url.trim_end_matches('/'));
    let mut req = client.post(&url).json(&body);

    if let Some(auth) = registration_auth() {
        req = req.header("Authorization", format!("Bearer {auth}"));
    }

    let resp = req.send().await?;

    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        return Err(format!("registration failed ({status}): {text}").into());
    }

    let reg: RegisterResponse = resp.json().await?;

    eprintln!("[registration] registered successfully");
    eprintln!("[registration]    node_id = {}", reg.node_id);

    Ok((reg.node_id, reg.node_token))
}
