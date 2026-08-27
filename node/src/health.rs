use std::time::Duration;

use crate::config::NodeConfig;

/// Ping the server forever. Meant to be spawned as a background task.
pub async fn health_loop(config: NodeConfig) {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .expect("failed to build HTTP client for health loop");

    let url = format!(
        "{}/nodes/health",
        config.server_url.trim_end_matches('/')
    );

    loop {
        let result = client
            .post(&url)
            .header("X-Node-Id", &config.node_id)
            .header("Authorization", format!("Bearer {}", config.node_token))
            .json(&serde_json::json!({}))
            .send()
            .await;

        match result {
            Ok(resp) if resp.status().is_success() => {}
            Ok(resp) => {
                eprintln!(
                    "[health] warning: server responded with {}",
                    resp.status()
                );
            }
            Err(e) => {
                eprintln!("[health] warning: ping failed — {e}");
            }
        }

        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}
