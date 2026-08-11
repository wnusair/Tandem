use std::fs;

use aes_gcm::aead::Aead;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use rsa::pkcs8::DecodePrivateKey;
use rsa::sha2::Sha256;
use rsa::{Oaep, RsaPrivateKey};
use sha2::Digest;

// ---------------------------------------------------------------------------
// RSA key loading
// ---------------------------------------------------------------------------

/// Load an RSA private key from a PKCS#8 PEM file.
pub fn load_private_key(path: &str) -> Result<RsaPrivateKey, Box<dyn std::error::Error>> {
    let pem = fs::read_to_string(path)?;
    let key = RsaPrivateKey::from_pkcs8_pem(&pem)?;
    Ok(key)
}

// ---------------------------------------------------------------------------
// DEK decryption (RSA-OAEP / SHA-256)
// ---------------------------------------------------------------------------

/// Decrypt a base64-encoded, RSA-OAEP-encrypted Data-Encryption Key.
pub fn decrypt_dek(
    private_key: &RsaPrivateKey,
    encrypted_dek_b64: &str,
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let ciphertext = B64.decode(encrypted_dek_b64)?;
    let padding = Oaep::new::<Sha256>();
    let dek = private_key.decrypt(padding, &ciphertext)?;
    Ok(dek)
}

// ---------------------------------------------------------------------------
// Blob decryption (AES-256-GCM)
// ---------------------------------------------------------------------------

/// Decrypt a WASM blob using AES-256-GCM.
///
/// * `dek`        — the raw 32-byte Data-Encryption Key
/// * `iv_b64`     — base64-encoded 12-byte nonce / IV
/// * `ciphertext` — the encrypted bytes (ciphertext || auth-tag)
pub fn decrypt_blob(
    dek: &[u8],
    iv_b64: &str,
    ciphertext: &[u8],
) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    let iv_bytes = B64.decode(iv_b64)?;
    let nonce = Nonce::from_slice(&iv_bytes);
    let cipher = Aes256Gcm::new_from_slice(dek)?;
    let plaintext = cipher
        .decrypt(nonce, ciphertext)
        .map_err(|e| format!("AES-GCM decryption failed: {e}"))?;
    Ok(plaintext)
}

// ---------------------------------------------------------------------------
// Receipt signing (RSA-PSS / SHA-256)
// ---------------------------------------------------------------------------

/// Produce a base64-encoded RSA-PSS signature over `message`.
pub fn sign_receipt(
    private_key: &RsaPrivateKey,
    message: &[u8],
) -> Result<String, Box<dyn std::error::Error>> {
    use rsa::pss::SigningKey;
    use rsa::signature::{SignatureEncoding, Signer};

    let signing_key = SigningKey::<Sha256>::new(private_key.clone());
    let signature = signing_key.sign(message);
    Ok(B64.encode(signature.to_bytes()))
}

// ---------------------------------------------------------------------------
// Hashing
// ---------------------------------------------------------------------------

/// SHA-256 hash of `data`, returned as a lowercase hex string.
pub fn sha256_hex(data: &[u8]) -> String {
    hex_encode(&sha256_bytes(data))
}

/// SHA-256 hash of `data` as raw bytes, for when you want a seed rather than
/// something to print.
pub fn sha256_bytes(data: &[u8]) -> [u8; 32] {
    sha2::Sha256::digest(data).into()
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        write!(s, "{b:02x}").unwrap();
    }
    s
}
