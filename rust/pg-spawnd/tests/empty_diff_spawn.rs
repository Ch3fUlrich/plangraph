#![cfg(unix)]
//! An empty review diff refuses once through the real MCP spawn, with the registry loaded the way
//! production loads it, before any route or launch adapter runs.
#[path = "common/binary.rs"]
mod binary;
mod common;
use common::{ChildGuard, McpClient, TempDir};
use serde_json::{json, Value};
use std::{os::unix::fs::PermissionsExt, path::Path};

fn git(dir: &Path, args: &[&str]) -> String {
    let out = std::process::Command::new("git")
        .current_dir(dir)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .args(["-c", "user.name=t", "-c", "user.email=t@example.invalid"])
        .args(args)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{args:?}: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().into()
}

fn marker_script(path: &Path, marker: &Path) {
    std::fs::write(
        path,
        format!(
            "#!/bin/sh\ntouch '{}'\ncat >/dev/null\nexit 1\n",
            marker.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

#[test]
fn empty_registry_diff_refuses_once_before_route_and_launch() {
    let state = TempDir::new();
    let origin = state.path().join("origin");
    std::fs::create_dir_all(&origin).unwrap();
    git(&origin, &["init", "-q", "-b", "main"]);
    std::fs::write(origin.join("a.txt"), "one\n").unwrap();
    git(&origin, &["add", "a.txt"]);
    git(&origin, &["commit", "-qm", "base"]);
    let base = git(&origin, &["rev-parse", "HEAD"]);
    let registry_checkout = state.path().join("checkout");
    git(
        state.path(),
        &[
            "clone",
            "-q",
            origin.to_str().unwrap(),
            registry_checkout.to_str().unwrap(),
        ],
    );
    // The head exists only in the origin: the registry checkout must fetch it to see the range is empty.
    git(&origin, &["commit", "-q", "--allow-empty", "-m", "head"]);
    let head = git(&origin, &["rev-parse", "HEAD"]);

    let (route, launch) = (state.path().join("route"), state.path().join("launch"));
    let (routed, launched) = (state.path().join("routed"), state.path().join("launched"));
    marker_script(&route, &routed);
    marker_script(&launch, &launched);
    let cfg_dir = state.path().join("xdg/plangraph");
    std::fs::create_dir_all(&cfg_dir).unwrap();
    let hosts = json!({"version": 1, "self": "local", "hosts": [{
        "id": "local", "class": "worker", "aliases": [], "transport": "ssh", "os": "linux",
        "state_dir": state.path(),
        "caps": {"max_runs": 8, "max_writers": 2, "max_seats": 4, "max_sessions": 4, "mem_floor_mb": 1024},
        "warmup": {"warmup_s": 1, "ramp_mb": {"review": 0}}, "session_mb": 256, "roles": ["review"],
        "repos": {"demo": {"root": registry_checkout, "worktrees_root": state.path().join("wt"),
            "remote": origin, "import_roots": [], "cache_env": {}}}}]});
    std::fs::write(cfg_dir.join("hosts.json"), hosts.to_string()).unwrap();

    let socket = state.path().join("socket");
    let child = std::process::Command::new(binary::spawnd())
        .args(["serve", "--no-scope", "--socket"])
        .arg(&socket)
        .arg("--state")
        .arg(state.path())
        .env("XDG_CONFIG_HOME", state.path().join("xdg"))
        .env("PG_SPAWND_ADAPTER", &route)
        .env("PG_SPAWND_LAUNCH_CMD", &launch)
        .spawn()
        .unwrap();
    let _guard = ChildGuard { child };
    let mut client = McpClient::connect(&socket);
    client.initialize();
    for diff_ref in [
        json!({"repo_id": "demo", "base_sha": head, "head_sha": head}),
        json!({"repo": registry_checkout, "base": head, "head": head}),
    ] {
        let reply: Value = client.request(
            "tools/call",
            json!({"name": "plangraph_agent_spawn", "arguments": {
                "role": "review", "prompt": "review", "writer_families": ["openai"],
                "card": {"estimate_tokens": 1000, "diff_ref": diff_ref}, "wait_seconds": 5}}),
        );
        let message = reply["error"]["message"]
            .as_str()
            .unwrap_or_else(|| panic!("{reply}"));
        assert!(message.starts_with("empty_diff:"), "{reply}");
    }
    assert_ne!(base, head);
    assert!(
        !routed.exists() && !launched.exists(),
        "no route or launch adapter may run"
    );
    let records: Vec<_> = std::fs::read_dir(state.path().join("runs"))
        .unwrap()
        .flatten()
        .collect();
    assert_eq!(records.len(), 2, "one record per refused card");
    for entry in records {
        let record: Value = serde_json::from_str(
            &std::fs::read_to_string(entry.path().join("record.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(record["reason"], "empty_diff", "{record}");
        assert_eq!(record["legs"], json!([]));
    }
}
