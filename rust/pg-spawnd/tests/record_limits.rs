//! Hostile record files are rejected by result/status and skipped by ps.
#![cfg(unix)]
mod common;

use common::TempDir;
use std::io::Write;

#[test]
fn hostile_records_are_invalid_and_skipped() {
    let tmp = TempDir::new();
    let runs = tmp.path().join("runs");
    for id in ["run-link", "run-dir", "run-large", "run-sparse"] {
        std::fs::create_dir_all(runs.join(id)).unwrap();
    }
    let target = tmp.path().join("target.json");
    std::fs::write(&target, br#"{"run_id":"run-link"}"#).unwrap();
    std::os::unix::fs::symlink(&target, runs.join("run-link/record.json")).unwrap();
    std::fs::create_dir(runs.join("run-dir/record.json")).unwrap();
    std::fs::File::create(runs.join("run-sparse/record.json"))
        .unwrap()
        .set_len(8 * 1024 * 1024 + 1)
        .unwrap();
    let mut large = std::fs::File::create(runs.join("run-large/record.json")).unwrap();
    large.set_len(8 * 1024 * 1024 + 1).unwrap();
    // Keep the oversized file valid JSON so removing the cap fails this test.
    large.write_all(br#"{"run_id":"run-large"}"#).unwrap();
    let prefix_len = br#"{"run_id":"run-large"}"#.len();
    large
        .write_all(&vec![b' '; 8 * 1024 * 1024 + 1 - prefix_len])
        .unwrap();
    let socket = tmp.path().join("socket");
    let guard = common::ChildGuard::spawn(
        env!("CARGO_BIN_EXE_pg-spawnd"),
        &[
            std::ffi::OsStr::new("serve"),
            std::ffi::OsStr::new("--socket"),
            socket.as_os_str(),
            std::ffi::OsStr::new("--state"),
            tmp.path().as_os_str(),
        ],
    );
    let mut client = common::McpClient::connect(&socket);
    client.initialize();
    let _guard = guard;
    let mut call = |name: &str, args: serde_json::Value| {
        client.request(
            "tools/call",
            serde_json::json!({"name":name,"arguments":args}),
        )
    };
    for id in ["run-link", "run-dir", "run-large", "run-sparse"] {
        let response = call("plangraph_agent_result", serde_json::json!({"run_id":id}));
        assert_eq!(response["error"]["message"], "invalid run record");
    }
    let response = call("plangraph_agent_ps", serde_json::json!({}));
    let text = response["result"]["content"][0]["text"].as_str().unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(text).unwrap(),
        serde_json::json!({"runs":[]})
    );
}
