//! CLI adapter to the existing admission gate, pending its Rust port.
use serde_json::Value;
use std::path::Path;
use std::process::Stdio;

// Only invoke the canonical helper here: no Git checks or policy belong in this
// adapter. The Python package must be installed, as for pg-spawnd's adapter.
const CHECK: &str = "import json, sys; from plangraph.spawner.base_fresh import base_fresh; print(json.dumps(base_fresh(sys.argv[1], ref=sys.argv[2], fetch=sys.argv[3] == '1')))";

pub use pg_worker_core::writer_families::python_command;

/// Run the canonical D-5661 gate on `git_ref` of `repo`; Err carries its one-line reason.
pub fn check_ref(
    repo: &str,
    git_ref: &str,
    fetch: bool,
    src_root: Option<&Path>,
) -> Result<(), String> {
    let result = python_command(src_root)
        .args(["-c", CHECK, repo, git_ref, if fetch { "1" } else { "0" }])
        .stdin(Stdio::null())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| serde_json::from_slice::<Value>(&output.stdout).ok());
    match result {
        Some(result) if result["ok"] == true && result["reason"].is_null() => Ok(()),
        Some(result) if result["ok"] == false => {
            // Keep the helper's reason verbatim, but preserve the one-line CLI
            // contract if its output is ever malformed.
            Err(result["reason"]
                .as_str()
                .filter(|reason| {
                    reason.starts_with("base_stale: ") && !reason.contains(['\r', '\n'])
                })
                .unwrap_or("base_stale: gate_unavailable")
                .to_owned())
        }
        _ => Err("base_stale: gate_unavailable".to_owned()),
    }
}

pub fn run(repo: &str) -> i32 {
    match check_ref(repo, "HEAD", true, None) {
        Ok(()) => {
            println!("base_current");
            0
        }
        Err(reason) => {
            eprintln!("{reason}");
            1
        }
    }
}
