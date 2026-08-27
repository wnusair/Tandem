mod config;
mod crypto;
mod executor;
mod health;
mod registration;
// The serve path needs bubblewrap, unix sockets, and libc rlimits, none of
// which exist on Windows. Compute still works there.
#[cfg(unix)]
mod sandbox;
#[cfg(unix)]
mod serve;
mod state;
mod worker;

use std::env;

use config::NodeConfig;
use state::NodeState;

#[tokio::main]
async fn main() {
    // Fall back to the parent directory so `cargo run` works from inside node/.
    if dotenvy::dotenv().is_err() {
        let _ = dotenvy::from_filename("../.env");
    }

    let mut cfg = NodeConfig::from_env();

    // The CLI uses this to register the machine and immediately exit, so it can
    // tell the user "registered as node_xyz" before it starts the long-running
    // background process. In this mode we skip the health and task loops.
    let register_only = env::var("TANDEM_NODE_REGISTER_ONLY")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    eprintln!("[node] server_url = {}", cfg.server_url);

    // First boot: no saved identity, so register.
    if cfg.node_id.is_empty() {
        eprintln!("[node] no saved node identity — starting registration…");
        match registration::register_node(&cfg.server_url, &cfg.private_key_path).await {
            Ok((node_id, node_token)) => {
                cfg.node_id = node_id;
                cfg.node_token = node_token;
                persist_identity(&cfg);
            }
            Err(e) => {
                eprintln!("[node] FATAL: registration failed — {e}");
                std::process::exit(1);
            }
        }
    } else if register_only {
        eprintln!("[node] already registered as {}", cfg.node_id);
    }

    eprintln!("[node] node_id = {}", cfg.node_id);

    // Registration done and reported — nothing else to do in this mode.
    if register_only {
        // A stdout marker the CLI can read as a fallback to the state file.
        println!("TANDEM_NODE_ID={}", cfg.node_id);
        return;
    }

    let private_key = match crypto::load_private_key(&cfg.private_key_path) {
        Ok(k) => k,
        Err(e) => {
            eprintln!(
                "[node] FATAL: could not load private key from '{}': {e}",
                cfg.private_key_path
            );
            std::process::exit(1);
        }
    };

    let health_cfg = cfg.clone();
    tokio::spawn(async move {
        health::health_loop(health_cfg).await;
    });

    // The web-hosting side: claim serve deployments and proxy their traffic.
    #[cfg(unix)]
    {
        let serve_cfg = cfg.clone();
        tokio::spawn(async move {
            serve::serve_loop(serve_cfg).await;
        });
    }

    eprintln!("[node] health loop started (every 3 s)");
    eprintln!("[node] entering task claim loop…");

    tokio::select! {
        _ = worker::task_loop(&cfg, &private_key) => {
            // task_loop runs forever; this arm only fires if it somehow returns.
        }
        _ = shutdown_signal() => {
            eprintln!("\n[node] shutdown signal received — exiting gracefully");
        }
    }
}

/// Wait for SIGINT (Ctrl-C) or SIGTERM.
async fn shutdown_signal() {
    use tokio::signal;

    let ctrl_c = async {
        signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {}
        _ = terminate => {}
    }
}

/// Save the node's identity so the next boot reuses it instead of registering
/// again. A failure here is only a warning — the node can still run this
/// session, it just won't remember who it is next time.
fn persist_identity(cfg: &NodeConfig) {
    let saved = NodeState {
        node_id: cfg.node_id.clone(),
        node_token: cfg.node_token.clone(),
    };

    match saved.save(&cfg.state_path) {
        Ok(()) => eprintln!("[node] identity saved to {}", cfg.state_path),
        Err(e) => eprintln!(
            "[node] warning: could not save identity to {}: {e}",
            cfg.state_path
        ),
    }
}