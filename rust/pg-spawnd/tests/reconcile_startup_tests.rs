#![cfg(unix)]
mod common;

use common::{ChildGuard, TempDir};
use std::ffi::OsStr;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn serve_with_broken_inventory(value: &OsStr, refuses: bool) {
    let state = TempDir::new();
    // An inventory root must be a directory, not a regular file.
    std::fs::write(state.path().join("runs"), b"broken").unwrap();
    let socket = state.path().join("socket");
    let mut guard = ChildGuard {
        child: Command::new(env!("CARGO_BIN_EXE_pg-spawnd"))
            .args(["serve", "--no-scope", "--socket"])
            .arg(&socket)
            .arg("--state")
            .arg(state.path())
            .env("XDG_CONFIG_HOME", state.path().join("config"))
            .env("PLANGRAPH_RELIABILITY_RECONCILE", value)
            .stderr(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap(),
    };
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        if let Some(status) = guard.child.try_wait().unwrap() {
            break Some(status);
        }
        if socket.exists() || Instant::now() >= deadline {
            break None;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    if status.is_none() {
        guard.child.kill().unwrap();
        guard.child.wait().unwrap();
    }
    let mut stderr = String::new();
    guard
        .child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();
    if refuses {
        assert_eq!(status.and_then(|s| s.code()), Some(2), "{stderr}");
        assert!(!socket.exists(), "refused startup created a socket");
    } else {
        assert!(
            socket.exists(),
            "startup did not bind: {status:?}: {stderr}"
        );
        assert!(
            stderr.contains("reconciliation startup skipped:"),
            "{stderr}"
        );
    }
}

#[test]
fn shadow_binds_despite_inventory_error() {
    serve_with_broken_inventory(OsStr::new("shadow"), false);
}

#[test]
fn garbage_binds_despite_mode_error() {
    serve_with_broken_inventory(OsStr::new("garbage"), false);
}

#[test]
fn non_utf8_binds_despite_mode_error() {
    serve_with_broken_inventory(OsStr::from_bytes(b"\xff"), false);
}

#[test]
fn on_refuses_without_socket() {
    serve_with_broken_inventory(OsStr::new("on"), true);
}
