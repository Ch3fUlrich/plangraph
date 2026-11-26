#![cfg(unix)]

use std::{fs, path::Path, process::Command};

#[test]
fn readiness_followup_rust_release_installs_train_cli_and_spawner_together() {
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
    let check_release = |head: &str, options: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_plangraph"))
            .arg("host-sync")
            .env_clear()
            .env("HOME", home)
            .env("PATH", "/usr/bin:/bin")
            .arg("--check-release")
            .arg(&repo)
            .arg(head)
            .args(options)
            .output()
            .unwrap()
    };
    let head = git(&["rev-parse", "HEAD"]);
    assert_eq!(check_release(&head, &[]).status.code(), Some(1));

    let config = home.join(".config/plangraph");
    fs::create_dir_all(&config).unwrap();
    std::os::unix::fs::symlink(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../src"),
        repo.join("src"),
    )
    .unwrap();
    fs::write(config.join("hosts.json"), r#"{"hosts":[{"id":"guest","os":"linux","transport":"ssh","ssh_target":"windows-hop","wsl_distro":"Ubuntu","caps":{"mem_floor_mb":4096},"repos":{"plangraph":{"root":"/srv/repo"}}}]}"#).unwrap();
    let stale = || {
        fs::write(
            config.join("ps-hosts.json"),
            r#"{"hosts":[{"name":"guest","target":"windows-hop","win_src":"C:/old/src"}]}"#,
        )
        .unwrap()
    };
    let assert_projections = || {
        let read = |name| {
            serde_json::from_slice::<serde_json::Value>(&fs::read(config.join(name)).unwrap())
                .unwrap()
        };
        let ps = read("ps-hosts.json");
        assert_eq!(ps["hosts"][0]["wsl_distro"], "Ubuntu");
        assert!(ps["hosts"][0].get("win_src").is_none());
        assert_eq!(
            read("overflow-hosts.json")["hosts"]["guest"]["wsl_distro"],
            "Ubuntu"
        );
    };
    stale();
    let output = run(&[]);
    assert!(output.status.success(), "{output:?}");
    assert_projections();
    assert!(bin.join("pg-spawnd").is_file());
    assert!(bin.join("pg-disk-hygiene").is_file());
    assert!(bin.join("pg-telemetry").is_file());
    assert!(String::from_utf8_lossy(&output.stdout).contains("RELEASE INSTALLED"));
    assert!(!bin.join("unit-breaker").exists());
    assert_eq!(
        fs::read_to_string(&stamp).unwrap().trim(),
        git(&["rev-parse", "HEAD:rust"])
    );
    stale();
    assert!(String::from_utf8_lossy(&run(&[]).stdout).contains("SAME"));
    assert_projections();

    let checked = check_release(&head, &[]);
    assert!(checked.status.success(), "{checked:?}");
    let receipt: serde_json::Value = serde_json::from_slice(&checked.stdout).unwrap();
    assert_eq!(receipt["status"], "current");
    assert_eq!(receipt["head"], head);
    assert_eq!(receipt["rust_tree"], git(&["rev-parse", "HEAD:rust"]));
    assert_eq!(receipt["binaries"].as_object().unwrap().len(), 5);
    assert_eq!(receipt["binaries"]["pg-spawnd"].as_str().unwrap().len(), 64);

    let artifact = bin.join("pg-telemetry");
    fs::write(&artifact, "replacement").unwrap();
    let mismatch = check_release(&head, &[]);
    assert_eq!(mismatch.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&mismatch.stderr).contains("pg-telemetry"));
    let repaired = run(&[]);
    assert!(repaired.status.success(), "{repaired:?}");
    assert!(String::from_utf8_lossy(&repaired.stdout).contains("RELEASE INSTALLED"));
    assert!(check_release(&head, &[]).status.success());
    assert_eq!(check_release(&base, &[]).status.code(), Some(1));

    // A legacy stamp with the right tree does not attest existing destination bytes.
    fs::remove_file(stamp.with_extension("json")).unwrap();
    fs::write(&artifact, "unverified legacy executable").unwrap();
    assert_eq!(check_release(&head, &[]).status.code(), Some(1));
    let migrated = run(&[]);
    assert!(migrated.status.success(), "{migrated:?}");
    assert!(String::from_utf8_lossy(&migrated.stdout).contains("RELEASE INSTALLED"));
    assert!(check_release(&head, &[]).status.success());

    let lock = pg_worker_core::lockfile::try_lock(&stamp.with_file_name("release-install.lock"))
        .unwrap()
        .unwrap();
    let busy = check_release(&head, &[]);
    assert_eq!(busy.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&busy.stderr).contains("release install"));
    drop(lock);

    let backup = bin.join("pg-keeper-backup");
    assert!(!backup.exists());
    let output = run(&["--with-keeper-backup"]);
    assert!(output.status.success(), "{output:?}");
    assert!(backup.is_file());
    assert!(String::from_utf8_lossy(&run(&["--with-keeper-backup"]).stdout).contains("SAME"));
    let checked = check_release(&head, &["--with-keeper-backup"]);
    assert!(checked.status.success(), "{checked:?}");
    let receipt: serde_json::Value = serde_json::from_slice(&checked.stdout).unwrap();
    assert!(receipt["binaries"]["pg-keeper-backup"].is_string());

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

    // The timer can advance Git without rebuilding when the Rust tree is unchanged.
    fs::write(repo.join("README.md"), "Documentation").unwrap();
    git(&["add", "README.md"]);
    git(&[
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "docs only",
    ]);
    let current = git(&["rev-parse", "HEAD"]);
    let checked = check_release(&current, &[]);
    assert!(checked.status.success(), "{checked:?}");
    let receipt: serde_json::Value = serde_json::from_slice(&checked.stdout).unwrap();
    assert_eq!(receipt["head"], current);
    assert_eq!(receipt["build_head"], head);
    assert_eq!(check_release(&head, &[]).status.code(), Some(1));
}

#[test]
fn a_host_binaries_mark_of_the_wrong_type_fails_the_install_before_any_build() {
    let root = tempfile::tempdir().unwrap();
    let home = root.path();
    let repo = home.join("repo");
    let tools = home.join("tools");
    let git = |args: &[&str]| {
        let output = Command::new("git")
            .arg("-C")
            .arg(&repo)
            .args([
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
            ])
            .args(args)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
    };
    fs::create_dir_all(repo.join("rust")).unwrap();
    git(&["init", "-q"]);
    fs::write(repo.join("rust/test.rs"), "// x\n").unwrap();
    git(&["add", "rust"]);
    git(&["commit", "-qm", "x"]);
    fs::create_dir_all(&tools).unwrap();
    // The fake cargo answers metadata only; a build would write this marker.
    pg_worker_core::executable_test_support::write_executable(
        tools.join("cargo"),
        "#!/bin/sh\nif [ \"$1\" = metadata ]; then cat \"$FIXTURE_METADATA\"; exit 0; fi\ntouch \"$HOME/built\"\n",
    );
    let metadata = home.join("metadata.json");
    fs::write(
        &metadata,
        serde_json::json!({"packages": [{
            "name": "pg-telemetry",
            "targets": [{"name": "pg-telemetry", "kind": ["bin"]}],
            "metadata": {"plangraph": {"host-binaries": "pg-telemetry"}},
        }]})
        .to_string(),
    )
    .unwrap();
    let path =
        std::env::join_paths([tools.as_path(), Path::new("/usr/bin"), Path::new("/bin")]).unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .env("PATH", &path)
        .env("CARGO_TARGET_DIR", home.join("target"))
        .env("FIXTURE_METADATA", &metadata)
        .arg("--install-release")
        .arg(&repo)
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("host-binaries must be a list"), "{stderr}");
    assert!(!home.join("built").exists());
    assert!(!home.join(".local/bin/pg-telemetry").exists());
}
