//! The compiled output of the engine: WASM bytes plus the metadata the rest of
//! Tandem needs to route, cache, and trust them.

use sha2::{Digest, Sha256};

/// Which flavor of WASM binary we produced.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ArtifactKind {
    /// A WebAssembly component (the wasip2 world). This is the new default.
    Component,
    /// A classic core WebAssembly module (the wasip1 world).
    CoreModule,
}

impl ArtifactKind {
    /// A short, stable name we can put in manifests, logs, and headers.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Component => "component",
            Self::CoreModule => "core-module",
        }
    }
}

/// A finished piece of compiled work ready to ship to a node.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Artifact {
    /// The raw WASM bytes.
    pub bytes: Vec<u8>,
    /// Whether these bytes are a component or a core module.
    pub kind: ArtifactKind,
    /// Hex-encoded SHA-256 of `bytes`, used as a cache key and for logging.
    pub content_hash: String,
}

impl Artifact {
    /// Wrap raw bytes into an artifact, computing the content hash for you.
    pub fn new(bytes: Vec<u8>, kind: ArtifactKind) -> Self {
        let content_hash = hash_bytes(&bytes);
        Self {
            bytes,
            kind,
            content_hash,
        }
    }

    /// How big the artifact is, in bytes.
    pub fn len(&self) -> usize {
        self.bytes.len()
    }

    /// True when there are no bytes at all.
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }
}

/// Hex SHA-256, shared by the artifact and the cache so a hash always means
/// the same thing.
pub fn hash_bytes(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    hex_encode(&digest)
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}
