//! The `cargo` link of the native writer sandbox holds the shared lease per command.
#![cfg(target_os = "linux")]
use pg_worker_core::cargo_cache::{lease_holders, try_exclusive_lease};
use pg_worker_core::cargo_command_lease::install_shim;
use std::{os::unix::fs::PermissionsExt, path::Path, process::Command, time::Duration};

fn script(dir: &Path, name: &str, body: &str) {
    let path = dir.join(name);
    std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
}

fn wait_for(mut done: impl FnMut() -> bool) -> bool {
    for _ in 0..500 {
        if done() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    false
}

#[test]
fn a_cancelled_cargo_command_strands_neither_the_lease_nor_the_cargo_process() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let pidfile = tmp.path().join("cargo.pid");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(
        &real,
        "cargo",
        &format!("echo $$ > {}\nexec sleep 300", pidfile.display()),
    );
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    let mut child = Command::new(&link)
        .arg("build")
        .env("CARGO_TARGET_DIR", &target)
        .env(
            "PATH",
            format!("{}:{}:/usr/bin:/bin", shim.display(), real.display()),
        )
        .env_remove("PLANGRAPH_CARGO_LEASE_HELD")
        .spawn()
        .unwrap();
    assert!(wait_for(
        || pidfile.exists() && !lease_holders(&target).is_empty()
    ));
    assert_eq!(lease_holders(&target), vec![child.id()]);
    assert!(try_exclusive_lease(&target).unwrap().is_none());
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(
        try_exclusive_lease(&target).unwrap().is_some(),
        "the lease survived the cancelled command"
    );
    let cargo: i32 = std::fs::read_to_string(&pidfile)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    // SAFETY: signal 0 only probes for existence.
    assert!(
        wait_for(|| unsafe { libc::kill(cargo, 0) } != 0),
        "the cargo child outlived its shim"
    );
}

#[test]
fn the_shim_reports_the_blocking_pid_when_the_target_is_being_reclaimed() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(&real, "cargo", "exit 0");
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    let _trim = try_exclusive_lease(&target).unwrap().unwrap();
    // The wait is bounded by the shim; cap it for the test through a command that has no outer lease.
    let output = Command::new(&link)
        .env("CARGO_TARGET_DIR", &target)
        .env("PATH", format!("{}:{}", shim.display(), real.display()))
        .env("PLANGRAPH_CARGO_SHIM_WAIT_MS", "0")
        .env_remove("PLANGRAPH_CARGO_LEASE_HELD")
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1));
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains(&format!("held by pid {}", std::process::id())),
        "{stderr}"
    );
}

#[test]
fn a_nested_command_under_an_outer_lease_does_not_wait_for_itself() {
    let tmp = tempfile::tempdir().unwrap();
    let target = tmp.path().join("target");
    let real = tmp.path().join("real");
    std::fs::create_dir_all(&real).unwrap();
    script(&real, "cargo", "exit 5");
    let shim = tmp.path().join("shim");
    let link = install_shim(&shim, Path::new(env!("CARGO_BIN_EXE_pg-spawnd"))).unwrap();
    // Preflight stages artifacts under the exclusive lease and sets the marker for its children.
    let _exclusive = try_exclusive_lease(&target).unwrap().unwrap();
    let status = Command::new(&link)
        .env("CARGO_TARGET_DIR", &target)
        .env("PLANGRAPH_CARGO_LEASE_HELD", &target)
        .env("PATH", format!("{}:{}", shim.display(), real.display()))
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(5));
}
