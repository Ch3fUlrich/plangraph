//! Exercise the guard's negative probes in the rust-worker CI job.
use std::{path::Path, process::Command};

#[test]
fn admission_guard_rejects_bypass_probes() {
    // The probe runs only the admission_policy_guard integration target, so it
    // cannot recursively invoke this test. CI supplies Rust and protoc here.
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap();
    let output = Command::new("python3")
        .arg(root.join("tools/probe_admission_policy_guard.py"))
        .current_dir(root)
        .output()
        .expect("run admission guard negative probes with python3");
    assert!(
        output.status.success(),
        "admission guard negative probes failed:\n{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
