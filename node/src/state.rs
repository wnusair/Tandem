use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

/// The node identity that survives a restart. Without it every boot would
/// register a fresh node_id, filling the server with dead nodes.
#[derive(Serialize, Deserialize, Default, Clone, Debug)]
pub struct NodeState {
    #[serde(default)]
    pub node_id: String,
    #[serde(default)]
    pub node_token: String,
}

impl NodeState {
    /// Read the saved identity. A missing or corrupt file means "never
    /// registered", which the caller handles by registering fresh.
    pub fn load(path: &str) -> Option<NodeState> {
        let text = fs::read_to_string(path).ok()?;
        serde_json::from_str(&text).ok()
    }

    /// Write the identity out, creating the parent directory if needed.
    pub fn save(&self, path: &str) -> Result<(), Box<dyn std::error::Error>> {
        if let Some(parent) = Path::new(path).parent()
            && !parent.as_os_str().is_empty()
        {
            fs::create_dir_all(parent)?;
        }
        let text = serde_json::to_string_pretty(self)?;
        fs::write(path, text)?;
        Ok(())
    }
}
