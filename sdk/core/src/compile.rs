//! The compile contract every language backend implements.
//!
//! The whole point of Tandem's engine is that adding a new language should mean
//! writing one small backend, not a new pipeline. So the shape of a compile
//! request, the errors, and the trait all live here, and each backend just
//! turns a `CompileRequest` into an `Artifact`.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::artifact::Artifact;
use crate::validate::validate_artifact;

/// A hard ceiling on how big a compiled artifact may be (256 MiB).
///
/// Componentize-py bundles a Python interpreter, so real artifacts are large,
/// but this stops an obviously broken or malicious build from filling the disk.
pub const MAX_ARTIFACT_BYTES: usize = 256 * 1024 * 1024;

/// Free-form knobs the SDK passes down to a backend (timeout_ms, memory_mb, ...).
///
/// We keep them as sorted string pairs so the same options always hash the same
/// way, which the build cache relies on.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct CompileOptions {
    pub values: BTreeMap<String, String>,
}

impl CompileOptions {
    pub fn new() -> Self {
        Self {
            values: BTreeMap::new(),
        }
    }

    /// Set one option, overwriting any previous value for that key.
    pub fn set(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.values.insert(key.into(), value.into());
    }
}

/// What the compiled task is for.
///
/// Compute tasks run once (stdin JSON in, stdout JSON out). Serve tasks are
/// long-lived web apps that the node hosts as a real process rather than as
/// WASM, so they're noted here but never actually lowered to WASM by a backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TaskShape {
    Compute,
    Serve,
}

impl TaskShape {
    /// A short, stable name for hashing and logging.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Compute => "compute",
            Self::Serve => "serve",
        }
    }
}

/// Everything a backend needs to turn user source into an artifact.
#[derive(Debug, Clone)]
pub struct CompileRequest {
    /// Which language backend should handle this (e.g. "python").
    pub language: String,
    /// Directory holding the user's source to compile.
    pub source_dir: PathBuf,
    /// The module to import (e.g. "app").
    pub entry_module: String,
    /// The function inside that module to run (e.g. "crunch").
    pub entry_function: String,
    /// Whether this is a compute task or a serve app.
    pub shape: TaskShape,
    /// Extra per-task options from the SDK.
    pub options: CompileOptions,
}

/// Anything that can go wrong while compiling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompileError {
    /// There was nothing to compile.
    EmptySource,
    /// The backend for this language isn't installed or available.
    BackendUnavailable(String),
    /// The backend ran but failed to produce a usable artifact.
    BackendFailed(String),
    /// The backend produced something that isn't a valid or allowed artifact.
    InvalidArtifact(String),
    /// A filesystem or process error while compiling.
    Io(String),
}

impl std::fmt::Display for CompileError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptySource => write!(formatter, "there was no source to compile"),
            Self::BackendUnavailable(msg) => {
                write!(formatter, "compile backend unavailable: {msg}")
            }
            Self::BackendFailed(msg) => write!(formatter, "compile backend failed: {msg}"),
            Self::InvalidArtifact(msg) => write!(formatter, "invalid compiled artifact: {msg}"),
            Self::Io(msg) => write!(formatter, "io error during compile: {msg}"),
        }
    }
}

impl std::error::Error for CompileError {}

/// The one thing a language backend has to implement.
///
/// Give it a request, get back an artifact. The engine takes care of caching
/// and validation around it (see `compile_with_cache` at the crate root), so a
/// backend only has to worry about the actual compile.
pub trait CompileBackend {
    /// Which language this backend handles, e.g. "python".
    fn language(&self) -> &str;

    /// Is the underlying toolchain actually installed and ready to run?
    fn is_available(&self) -> bool;

    /// Turn a request into an artifact.
    fn compile(&self, request: &CompileRequest) -> Result<Artifact, CompileError>;
}

/// Validate whatever a backend produced against the engine's trust rules and
/// wrap it up as an `Artifact`.
///
/// Backends call this so no unchecked bytes ever reach a node, and so every
/// backend shares exactly the same checks.
pub fn finalize_artifact(bytes: Vec<u8>) -> Result<Artifact, CompileError> {
    let kind = validate_artifact(&bytes, MAX_ARTIFACT_BYTES)?;
    Ok(Artifact::new(bytes, kind))
}
