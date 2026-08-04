//! PyO3 bridge that exposes the Tandem compile engine to Python.
//!
//! This wrapper is the thin "language binding" layer: Python code calls in
//! here, and everything real happens in the `tandem_core` crate. For now it
//! exposes the artifact check so the wrapper and the core are always built and
//! tested together; the full `compile` entry point lands once the language
//! backends are wired up.

// The #[pyfunction] macro expansion below inserts a same-type conversion that
// clippy flags as useless; it's generated code, not something we can rewrite.
#![allow(clippy::useless_conversion)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tandem_core::detect_kind;

/// Report whether some WASM bytes are a "component" or a "core-module".
///
/// Handy for tooling and tests, and it keeps the Python wrapper exercising the
/// real Rust core end to end.
#[pyfunction]
#[pyo3(text_signature = "(wasm_bytes)")]
fn artifact_kind(wasm_bytes: &[u8]) -> PyResult<String> {
    let kind = detect_kind(wasm_bytes).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(kind.as_str().to_string())
}

/// Python module exported by maturin/PyO3.
#[pymodule]
fn tandem_native(_python: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(artifact_kind, module)?)?;
    Ok(())
}
