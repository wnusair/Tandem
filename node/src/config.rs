use std::env;

use crate::state::NodeState;

#[derive(Clone, Debug)]
pub struct NodeConfig {
    pub server_url: String,
    pub node_id: String,
    pub node_token: String,
    pub private_key_path: String,
    pub state_path: String,
}

impl NodeConfig {
    /// Build configuration from environment variables, falling back to the saved
    /// identity file for the node_id/node_token when they aren't in the env.
    ///
    /// Required:
    ///   TANDEM_SERVER_URL — base URL of the Tandem server (e.g. "http://localhost:8080")
    ///
    /// Optional:
    ///   TANDEM_NODE_ID          — set after registration (env wins over the state file)
    ///   TANDEM_NODE_TOKEN       — set after registration (env wins over the state file)
    ///   TANDEM_PRIVATE_KEY_PATH — path to RSA private key PEM (default: "./node_key.pem")
    ///   TANDEM_NODE_STATE_PATH  — where the saved identity lives (default: "./node_state.json")
    pub fn from_env() -> Self {
        let server_url = env::var("TANDEM_SERVER_URL")
            .expect("TANDEM_SERVER_URL must be set in the environment or .env file");

        let state_path =
            env::var("TANDEM_NODE_STATE_PATH").unwrap_or_else(|_| "./node_state.json".to_string());

        // What we saved when we last registered. Env vars still win.
        let saved = NodeState::load(&state_path);

        let node_id = env_or_saved("TANDEM_NODE_ID", saved.as_ref().map(|s| s.node_id.as_str()));
        let node_token = env_or_saved(
            "TANDEM_NODE_TOKEN",
            saved.as_ref().map(|s| s.node_token.as_str()),
        );

        let private_key_path =
            env::var("TANDEM_PRIVATE_KEY_PATH").unwrap_or_else(|_| "./node_key.pem".to_string());

        Self {
            server_url,
            node_id,
            node_token,
            private_key_path,
            state_path,
        }
    }
}

/// Prefer a non-empty environment variable, then a non-empty saved value, then
/// an empty string (which the caller reads as "not registered yet").
fn env_or_saved(env_key: &str, saved: Option<&str>) -> String {
    if let Ok(value) = env::var(env_key)
        && !value.is_empty()
    {
        return value;
    }
    saved
        .filter(|v| !v.is_empty())
        .map(|v| v.to_string())
        .unwrap_or_default()
}
