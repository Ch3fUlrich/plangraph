use super::sandbox::{Launcher, Sandbox, REMOTE};
use serde_json::{json, Value};
use std::fs;

fn hold(s: &Sandbox, id: &str) {
    let dir = s.path("xdg/plangraph/worker-hosts");
    fs::create_dir_all(&dir).unwrap();
    fs::write(
        dir.join(format!("{id}.json")),
        r#"{"phase":"rejoining","missed":0,"observed_at":0,"needs_rejoin":true}"#,
    )
    .unwrap();
}

fn registry(s: &Sandbox) {
    let entry = |id: &str, transport: &str, aliases: Value| {
        json!({
            "id": id, "aliases": aliases, "transport": transport, "os": "linux",
            "state_dir": s.path("state"), "session_mb": 1, "roles": ["implement"],
            "caps": {"max_runs": 2, "max_writers": 1, "max_seats": 1, "max_sessions": 1, "mem_floor_mb": 1},
            "warmup": {"warmup_s": 1, "ramp_mb": {"implement": 1}}, "repos": {}
        })
    };
    s.write_json(
        "home/.config/plangraph/hosts.json",
        json!({"version": 1, "self": "observer", "hosts": [
            entry("observer", "local", json!([])),
            entry("fixture-worker", "ssh", json!([REMOTE])),
        ]}),
    );
}

fn setup(launcher: &Launcher) -> Sandbox {
    let s = Sandbox::new(launcher);
    s.hosts(json!([
        {"name": "fixture-local", "memory_floor_gib": 0, "last_resort": true},
        {"name": "fixture-other", "ssh": "fixture-other", "memory_floor_gib": 4},
        {"name": REMOTE, "ssh": "fixture-hop", "shell": "bash", "memory_floor_gib": 1},
    ]));
    fs::write(s.path("memory-fixture-other"), "MemAvailable: 8388608 kB\n").unwrap();
    fs::write(s.path("memory-fixture-hop"), "MemAvailable: 33554432 kB\n").unwrap();
    fs::write(s.path("ssh.stdout"), "forwarded dry run\n").unwrap();
    s
}

fn refused_named(s: &Sandbox, name: &str) {
    let out = s
        .launch("writer")
        .args(["--host", name, "--dry-run"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(2));
    let err = s.normalize(&out.stderr);
    assert!(
        err.contains(&format!("refused host {name}: worker host rejoining")),
        "{err}"
    );
}

/// A worker the coordinator holds in `rejoining` is never selected, named or not.
pub fn check(launcher: &Launcher) {
    let s = setup(launcher);
    hold(&s, REMOTE);
    let out = s.launch("writer").arg("--dry-run").output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert_ne!(s.json("ssh.json")["payload"]["host"], REMOTE);
    refused_named(&s, REMOTE);
}

/// The hold is kept under the registry id; the policy names the worker by its alias.
pub fn check_alias(launcher: &Launcher) {
    let s = setup(launcher);
    registry(&s);
    hold(&s, "fixture-worker");
    let out = s.launch("writer").arg("--dry-run").output().unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    assert_ne!(s.json("ssh.json")["payload"]["host"], REMOTE);
    refused_named(&s, REMOTE);
}

/// A forwarded launch carries the claim epoch in the receiver's environment.
pub fn check_forwarded_epoch(launcher: &Launcher) {
    let s = setup(launcher);
    let out = s
        .launch("writer")
        .args(["--host", REMOTE, "--dry-run"])
        .env("PG_EPOCH", "11")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{}", s.normalize(&out.stderr));
    let argv = s.json("ssh.json")["argv"].clone();
    let command = argv.as_array().unwrap().last().unwrap().as_str().unwrap();
    assert!(command.contains("PG_EPOCH=11 python3 -c "), "{command}");
}
