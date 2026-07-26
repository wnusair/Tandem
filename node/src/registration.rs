use std::env;
use std::fs;

use rand::rngs::OsRng;
use rsa::pkcs8::{EncodePrivateKey, EncodePublicKey, LineEnding};
use rsa::RsaPrivateKey;
use serde::{Deserialize, Serialize};

const RSA_KEY_BITS: usize = 4096;

#[derive(Serialize)]
struct RegisterRequest {
    supports_wasm: bool,
    rsa_public_key_pem: String,
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

    // Persist private key in PKCS#8 PEM format.
    let pem = private_key.to_pkcs8_pem(LineEnding::LF)?;
    fs::write(private_key_path, pem.as_bytes())?;
    eprintln!("[registration] private key saved to {private_key_path}");

    // Derive public key PEM for the registration payload.
    let pub_pem = private_key
        .to_public_key()
        .to_public_key_pem(LineEnding::LF)?;

    let body = RegisterRequest {
        supports_wasm: true,
        rsa_public_key_pem: pub_pem,
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
