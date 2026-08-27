//! A compile backend that drives `componentize-py` to turn a Python task into a
//! WASM component: wrap the user's function in a shim that speaks Tandem's
//! `run` contract, invoke the toolchain, hand the bytes back to the engine.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::artifact::Artifact;
use crate::compile::{finalize_artifact, CompileBackend, CompileError, CompileRequest};

/// The shim we generate and hand to componentize-py, which expects a `WitWorld`
/// class implementing the target world's exports.
const ENTRY_SHIM_TEMPLATE: &str = r#"import json
import importlib

_user_module = importlib.import_module("__MODULE__")
_marked = getattr(_user_module, "__FUNCTION__")

# Tandem's @compute decorator keeps the original function around as
# __tandem_original__. Inside the compiled component we always want that raw
# function, never the wrapper (which would try to dispatch back out to a node).
_user_function = getattr(_marked, "__tandem_original__", _marked)


class WitWorld:
    def run(self, task_input: bytes) -> bytes:
        args, kwargs = json.loads(task_input)
        result = _user_function(*args, **kwargs)
        return json.dumps(result).encode("utf-8")
"#;

/// Hands Python tasks to componentize-py to produce WASM components.
pub struct ComponentizePyBackend {
    /// How to invoke componentize-py. A list, so a caller can also pass
    /// something like `["python", "-m", "componentize_py"]`.
    command: Vec<String>,
    /// Directory that contains Tandem's WIT (the folder holding `task.wit`).
    wit_dir: PathBuf,
}

impl ComponentizePyBackend {
    pub fn new(command: Vec<String>, wit_dir: impl Into<PathBuf>) -> Self {
        Self {
            command,
            wit_dir: wit_dir.into(),
        }
    }

    /// Start building a `std::process::Command` from the configured invocation.
    fn base_command(&self) -> Command {
        let mut parts = self.command.iter();
        let program = parts.next().map(String::as_str).unwrap_or("componentize-py");
        let mut command = Command::new(program);
        for arg in parts {
            command.arg(arg);
        }
        command
    }
}

impl CompileBackend for ComponentizePyBackend {
    fn language(&self) -> &str {
        "python"
    }

    fn is_available(&self) -> bool {
        self.base_command()
            .arg("--version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false)
    }

    fn compile(&self, request: &CompileRequest) -> Result<Artifact, CompileError> {
        // A fresh temp dir, so the shim and output never touch the user's project.
        let work_dir = make_temp_dir()?;
        let result = self.compile_in(&work_dir, request);
        let _ = fs::remove_dir_all(&work_dir);
        result
    }
}

impl ComponentizePyBackend {
    /// Split out of `compile` so the temp directory is cleaned up either way.
    fn compile_in(
        &self,
        work_dir: &Path,
        request: &CompileRequest,
    ) -> Result<Artifact, CompileError> {
        write_entry_shim(work_dir, request)?;

        let out_path = work_dir.join("task.wasm");
        let mut command = self.base_command();
        command
            .arg("-d")
            .arg(&self.wit_dir)
            .arg("-w")
            .arg("task")
            .arg("componentize")
            .arg("_tandem_entry")
            .arg("-p")
            .arg(work_dir)
            .arg("-p")
            .arg(&request.source_dir)
            .arg("-o")
            .arg(&out_path);

        let output = command
            .output()
            .map_err(|error| CompileError::BackendUnavailable(error.to_string()))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(CompileError::BackendFailed(stderr.trim().to_string()));
        }

        let bytes = fs::read(&out_path).map_err(|error| CompileError::Io(error.to_string()))?;
        finalize_artifact(bytes)
    }
}

/// Write the `_tandem_entry.py` shim into `dir`, pointed at the user's function.
fn write_entry_shim(dir: &Path, request: &CompileRequest) -> Result<(), CompileError> {
    let shim = ENTRY_SHIM_TEMPLATE
        .replace("__MODULE__", &request.entry_module)
        .replace("__FUNCTION__", &request.entry_function);
    let path = dir.join("_tandem_entry.py");
    fs::write(&path, shim).map_err(|error| CompileError::Io(error.to_string()))
}

/// Counter that keeps temp directory names unique within a single process.
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Make a fresh temp directory for one compile.
fn make_temp_dir() -> Result<PathBuf, CompileError> {
    let unique = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("tandem-compile-{}-{}", std::process::id(), unique));
    fs::create_dir_all(&dir).map_err(|error| CompileError::Io(error.to_string()))?;
    Ok(dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::compile::{CompileOptions, TaskShape};

    #[test]
    fn shim_targets_the_requested_function() {
        let request = CompileRequest {
            language: "python".to_string(),
            source_dir: PathBuf::from("/tmp/whatever"),
            entry_module: "my_app".to_string(),
            entry_function: "do_work".to_string(),
            shape: TaskShape::Compute,
            options: CompileOptions::new(),
        };

        let dir = make_temp_dir().unwrap();
        write_entry_shim(&dir, &request).unwrap();
        let shim = fs::read_to_string(dir.join("_tandem_entry.py")).unwrap();
        let _ = fs::remove_dir_all(&dir);

        assert!(shim.contains("import_module(\"my_app\")"));
        assert!(shim.contains("getattr(_user_module, \"do_work\")"));
        assert!(!shim.contains("__MODULE__"));
        assert!(!shim.contains("__FUNCTION__"));
    }
}
