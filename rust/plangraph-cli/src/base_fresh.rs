//! CLI adapter to the existing admission gate, pending its Rust port.
use serde_json::Value;
use std::process::{Command, Stdio};

// Only invoke the canonical helper here: no Git checks or policy belong in this
// adapter. The Python package must be installed, as for pg-spawnd's adapter.
const CHECK: &str = "import json, sys; from plangraph.spawner.base_fresh import base_fresh; print(json.dumps(base_fresh(sys.argv[1])))";

pub fn run(repo: &str) -> i32 {
    let result = Command::new(
        std::env::var_os("PG_SPAWND_PYTHON")
            .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into()),
    )
    .args(["-c", CHECK, repo])
    .stdin(Stdio::null())
    .output()
    .ok()
    .filter(|output| output.status.success())
    .and_then(|output| serde_json::from_slice::<Value>(&output.stdout).ok());
    match result {
        Some(result) if result["ok"] == true && result["reason"].is_null() => {
            println!("base_current");
            0
        }
        Some(result) if result["ok"] == false => {
            // Keep the helper's reason verbatim, but preserve the one-line CLI
            // contract if its output is ever malformed.
            let reason = result["reason"]
                .as_str()
                .filter(|reason| {
                    reason.starts_with("base_stale: ") && !reason.contains(['\r', '\n'])
                })
                .unwrap_or("base_stale: gate_unavailable");
            eprintln!("{reason}");
            1
        }
        _ => {
            eprintln!("base_stale: gate_unavailable");
            1
        }
    }
}
