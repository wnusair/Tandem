//! Cheap, dependency-free checks on WASM bytes before we trust them. Both
//! questions we care about -- is this WASM, and is it a component or a core
//! module -- are answered by the first eight bytes.

use crate::artifact::ArtifactKind;
use crate::compile::CompileError;

/// Every WebAssembly binary starts with this 4-byte magic number: `\0asm`.
const WASM_MAGIC: [u8; 4] = [0x00, 0x61, 0x73, 0x6d];

/// A core module declares version 1 in the next four bytes.
const CORE_MODULE_VERSION: [u8; 4] = [0x01, 0x00, 0x00, 0x00];

/// A component declares version 0x0d with layer 1 in the next four bytes.
const COMPONENT_VERSION: [u8; 4] = [0x0d, 0x00, 0x01, 0x00];

/// Look at the binary preamble and decide whether these bytes are a component
/// or a core module. Returns an error if they aren't WASM at all.
pub fn detect_kind(bytes: &[u8]) -> Result<ArtifactKind, CompileError> {
    if bytes.len() < 8 {
        return Err(CompileError::InvalidArtifact(
            "wasm binary is too short to have a header".to_string(),
        ));
    }

    if bytes[0..4] != WASM_MAGIC {
        return Err(CompileError::InvalidArtifact(
            "bytes do not start with the wasm magic number".to_string(),
        ));
    }

    let version = &bytes[4..8];
    if version == CORE_MODULE_VERSION {
        Ok(ArtifactKind::CoreModule)
    } else if version == COMPONENT_VERSION {
        Ok(ArtifactKind::Component)
    } else {
        Err(CompileError::InvalidArtifact(format!(
            "unrecognized wasm version bytes: {version:?}"
        )))
    }
}

/// Run the full set of trust checks we want before caching or shipping an
/// artifact: it has to be real WASM, and it has to fit inside `max_size_bytes`
/// so a runaway or malicious compile can't hand us something enormous.
pub fn validate_artifact(
    bytes: &[u8],
    max_size_bytes: usize,
) -> Result<ArtifactKind, CompileError> {
    if bytes.len() > max_size_bytes {
        return Err(CompileError::InvalidArtifact(format!(
            "artifact is {} bytes, larger than the {} byte limit",
            bytes.len(),
            max_size_bytes
        )));
    }
    detect_kind(bytes)
}
