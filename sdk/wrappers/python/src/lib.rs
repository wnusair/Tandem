//! PyO3 bridge that exposes the Tandem compile engine to Python. Everything
//! real happens in `tandem_core`; for now only the artifact check is exposed,
//! with the full `compile` entry point landing once the backends are wired up.

// #[pyfunction] expands to a same-type conversion clippy flags as useless.
#![allow(clippy::useless_conversion)]

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use tandem_core::detect_kind;

/// Report whether some WASM bytes are a "component" or a "core-module".
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
