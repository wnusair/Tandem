use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

/// The bits of node identity we want to survive a restart. Without this the node
/// would ask the server for a brand-new node_id every time it booted, which
/// means the server would slowly fill up with dead nodes and the CLI could never
/// point at "the node running on this machine".
#[derive(Serialize, Deserialize, Default, Clone, Debug)]
pub struct NodeState {
    #[serde(default)]
    pub node_id: String,
    #[serde(default)]
    pub node_token: String,
}

impl NodeState {
    /// Read the saved identity, or None if the file is missing or unreadable.
    /// A missing or corrupt file just means "we've never registered", which the
    /// caller handles by registering fresh — so we don't treat it as an error.
    pub fn load(path: &str) -> Option<NodeState> {
        let text = fs::read_to_string(path).ok()?;
        serde_json::from_str(&text).ok()
    }

    /// Write the identity out as pretty JSON, creating the parent directory if
    /// it isn't there yet.
    pub fn save(&self, path: &str) -> Result<(), Box<dyn std::error::Error>> {
        if let Some(parent) = Path::new(path).parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        let text = serde_json::to_string_pretty(self)?;
        fs::write(path, text)?;
        Ok(())
    }
}
