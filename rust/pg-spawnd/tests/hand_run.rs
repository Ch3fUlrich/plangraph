#![cfg(target_os = "linux")]

use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Command,
};

struct Fixture {
    root: tempfile::TempDir,
    checkout: PathBuf,
    target: PathBuf,
    bin: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let checkout = root.path().join("checkout");
        let target = root.path().join("cargo-target");
        let bin = root.path().join("bin");
        for dir in [&checkout, &target, &bin] {
            fs::create_dir(dir).unwrap();
        }
        Self {
            root,
            checkout,
            target,
            bin,
        }
    }

    fn executable(&self, name: &str, body: &str) -> PathBuf {
        let path = self.bin.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        path
    }

    fn command(&self, target: Option<&Path>) -> Command {
        let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-spawnd"));
        cmd.current_dir(&self.checkout)
            .env_clear()
            .env("HOME", self.root.path())
            .env("PATH", &self.bin)
            .env("PLANGRAPH_CARGO_TARGET_DIR", &self.target)
            .env("FIXTURE_MARKER", self.root.path().join("spawned"));
        if let Some(target) = target {
            cmd.env("CARGO_TARGET_DIR", target);
        }
        cmd.arg("hand-run");
        cmd
    }
}

#[test]
fn writer_refuses_missing_empty_or_checkout_local_target_before_spawn() {
    let fixture = Fixture::new();
    let marker = fixture.root.path().join("spawned");
    let writer = fixture.executable(
        "codex",
        r#"printf '%s' "$CARGO_TARGET_DIR" > "$FIXTURE_MARKER""#,
    );
    let local = fixture.checkout.join("rust/target");
    for value in [None, Some(Path::new("")), Some(local.as_path())] {
        let output = fixture
            .command(value)
            .arg("--")
            .arg(&writer)
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("CARGO_TARGET_DIR"));
        assert!(!marker.exists());
    }
    let output = fixture
        .command(Some(&fixture.target))
        .arg("--")
        .arg(&writer)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        fs::read_to_string(marker).unwrap(),
        fixture.target.display().to_string()
    );
}
