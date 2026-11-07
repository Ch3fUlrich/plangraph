//! Exercise reconciliation through the actual daemon startup path.
#![cfg(unix)]
mod common;
use common::{ChildGuard, TempDir};
use std::{
    process::Command,
    time::{Duration, Instant},
};

#[test]
fn configured_reconcile_through_serve() {
    for config in [
        "{not json",
        r#"{"reconcile":true}"#,
        r#"{"reconcile":"on"}"#,
    ] {
        for explicit_on in [false, true] {
            let tmp = TempDir::new();
            let config_dir = tmp.path().join("plangraph");
            std::fs::create_dir(&config_dir).unwrap();
            std::fs::write(config_dir.join("reliability.json"), config).unwrap();
            let socket = tmp.path().join("socket");
            let state = tmp.path().join("state");
            let mut command = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
            command
                .arg("serve")
                .arg("--socket")
                .arg(&socket)
                .arg("--state")
                .arg(&state)
                .env("XDG_CONFIG_HOME", tmp.path())
                .env_remove("PLANGRAPH_RELIABILITY_RECONCILE");
            if explicit_on {
                command.env("PLANGRAPH_RELIABILITY_RECONCILE", "on");
            }
            let mut guard = ChildGuard {
                child: command.spawn().unwrap(),
            };
            let deadline = Instant::now() + Duration::from_secs(2);
            loop {
                if let Some(status) = guard.child.try_wait().unwrap() {
                    assert!(explicit_on, "default off refused {config}");
                    assert_eq!(status.code(), Some(2));
                    assert!(!socket.exists());
                    break;
                }
                if !explicit_on && socket.exists() {
                    break;
                }
                assert!(
                    Instant::now() < deadline,
                    "serve startup deadline (on={explicit_on})"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}
