#![cfg(unix)]

use std::{fs, path::Path, process::Command};

#[test]
fn rust_release_installs_train_cli_and_spawner_together() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path();
    let repo = home.join("repo");
    let tools = home.join("tools");
    let bin = home.join(".local/bin");
    let git = |args: &[&str]| {
        let output = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        String::from_utf8_lossy(&output.stdout).trim().to_owned()
    };
    fs::create_dir_all(repo.join("rust")).unwrap();
    git(&["init", "-q"]);
    fs::write(repo.join("rust/test.rs"), "// before\n").unwrap();
    git(&["add", "rust"]);
    git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "before",
    ]);
    let base = git(&["rev-parse", "HEAD"]);
    let before = git(&["rev-parse", "HEAD:rust"]);
    git(&["update-ref", "refs/remotes/origin/main", &base]);
    fs::write(repo.join("rust/test.rs"), "// after\n").unwrap();
    git(&["add", "rust"]);
    git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "after",
    ]);

    let stamp = home.join(".local/state/plangraph/release-built-tree");
    fs::create_dir_all(stamp.parent().unwrap()).unwrap();
    fs::write(&stamp, before).unwrap();
    let cargo = tools.join("cargo");
    fs::create_dir_all(&tools).unwrap();
    pg_worker_core::executable_test_support::write_executable(
        &cargo,
        r#"#!/bin/sh
set -eu
if [ "$1" = metadata ]; then
  cat "$FIXTURE_METADATA"
  exit 0
fi
mkdir -p "$CARGO_TARGET_DIR/release"
while [ "$#" -gt 0 ]; do
  if [ "$1" = --bin ]; then
    shift
    cp "$FIXTURE_BINARY" "$CARGO_TARGET_DIR/release/$1"
  fi
  shift
done
"#,
    );
    // The install plan comes from the workspace's cargo metadata: a crate that marks itself
    // (pg-telemetry here) is installed with no change to the installer.
    let metadata = home.join("metadata.json");
    let package = |name: &str, bins: &[&str], mark: &str, list: &[&str]| {
        let targets: Vec<_> = bins
            .iter()
            .map(|bin| serde_json::json!({"name": bin, "kind": ["bin"]}))
            .collect();
        serde_json::json!({
            "name": name,
            "targets": targets,
            "metadata": {"plangraph": {mark: list}},
        })
    };
    fs::write(
        &metadata,
        serde_json::json!({"packages": [
            package("pg-spawnd", &["pg-spawnd", "ci-temporal"], "host-binaries", &["pg-spawnd"]),
            package("plangraph-cli", &["plangraph", "host-sync"], "host-binaries", &["plangraph"]),
            package("pg-worker-core", &["pg-disk-hygiene", "turn-budget-hook", "unit-breaker"], "host-binaries", &["pg-disk-hygiene", "turn-budget-hook"]),
            package("pg-telemetry", &["pg-telemetry"], "host-binaries", &["pg-telemetry"]),
            package("pg-keeper-backup", &["pg-keeper-backup"], "optional-host-binaries", &["pg-keeper-backup"]),
        ]})
        .to_string(),
    )
    .unwrap();
    let target = home.join("fixture-artifacts");
    let path = std::env::join_paths([
        bin.as_path(),
        tools.as_path(),
        Path::new("/usr/bin"),
        Path::new("/bin"),
    ])
    .unwrap();
    let run = |options: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_host-sync"))
            .env_clear()
            .env("HOME", home)
            .env("PATH", &path)
            .env("CARGO_TARGET_DIR", &target)
            .env("FIXTURE_BINARY", env!("CARGO_BIN_EXE_plangraph"))
            .env("FIXTURE_METADATA", &metadata)
            .arg("--install-release")
            .arg(&repo)
            .args(options)
            .output()
            .unwrap()
    };
    let output = run(&[]);
    assert!(output.status.success(), "{output:?}");
    assert!(bin.join("pg-spawnd").is_file());
    assert!(bin.join("pg-disk-hygiene").is_file());
    assert!(bin.join("pg-telemetry").is_file());
    assert!(String::from_utf8_lossy(&output.stdout).contains("RELEASE INSTALLED"));
    assert!(!bin.join("unit-breaker").exists());
    assert_eq!(
        fs::read_to_string(&stamp).unwrap().trim(),
        git(&["rev-parse", "HEAD:rust"])
    );
    assert!(String::from_utf8_lossy(&run(&[]).stdout).contains("SAME"));

    let backup = bin.join("pg-keeper-backup");
    assert!(!backup.exists());
    let output = run(&["--with-keeper-backup"]);
    assert!(output.status.success(), "{output:?}");
    assert!(backup.is_file());
    assert!(String::from_utf8_lossy(&run(&["--with-keeper-backup"]).stdout).contains("SAME"));

    let heads = home.join("heads");
    let part = git(&["rev-parse", "HEAD"]);
    let tree = git(&["rev-parse", "HEAD^{tree}"]);
    let tip = git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        &tree,
        "-p",
        &base,
        "-p",
        &part,
        "-m",
        "Integrate Rust change",
    ]);
    fs::write(&heads, format!("{part}\n")).unwrap();
    let check = Command::new("/bin/sh")
        .env("PATH", &path)
        .args([
            "-c",
            "exec plangraph land-train check-chain --repo \"$1\" --head \"$2\" --heads \"$3\"",
            "fixture",
        ])
        .arg(&repo)
        .arg(tip)
        .arg(&heads)
        .output()
        .unwrap();
    assert!(check.status.success(), "{check:?}");
}
