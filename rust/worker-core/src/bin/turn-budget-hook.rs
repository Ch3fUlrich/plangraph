//! Claude Code command hook: JSON on stdin, silence on allow, exit 2 on poll.
use pg_worker_core::turn_budget::{handle, Config};
use std::io::Read;

fn main() {
    let mut bytes = Vec::new();
    if std::io::stdin()
        .take(8 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() > 8 * 1024 * 1024
    {
        return;
    }
    let Ok(payload) = serde_json::from_slice(&bytes) else {
        return;
    };
    // Even without HOME/state configuration, recognised sleep polling is denied.
    let config = Config::from_env().unwrap_or(Config {
        state_dir: std::path::PathBuf::new(),
        launch_dir: std::path::PathBuf::new(),
        session_name: None,
    });
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    if let Some(line) = handle(&payload, &config, now) {
        eprintln!("{line}");
        std::process::exit(2);
    }
}
