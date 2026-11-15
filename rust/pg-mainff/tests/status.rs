#![cfg(unix)]
mod common;
use common::{git, Fixture};
use pg_mainff::{advance, process::System};
use std::os::unix::fs::PermissionsExt;
use std::{fs, path::Path, process::Command};
fn drift(f: &Fixture, json: bool) -> String {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let registry = f._temp.path().join("hosts.json");
    fs::write(&registry,serde_json::json!({"hosts":[{"id":"fixture","transport":"local","repos":{"plangraph":{"live_dir":f.repo}}}]}).to_string()).unwrap();
    let mut cmd = Command::new("python3");
    cmd.arg("-c").arg("from plangraph.fleet_sync.__main__ import main; from plangraph.fleet_sync import release; from pathlib import Path; import sys; release.release_path=lambda: Path(sys.argv[1]); sys.exit(main(sys.argv[2:]))")
        .arg(f._temp.path().join("missing-release.json")).args(["drift","--registry",registry.to_str().unwrap()]).env("PYTHONPATH",root.join("src"));
    if json {
        cmd.arg("--json");
    }
    let result = cmd.output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    String::from_utf8(result.stdout).unwrap()
}
#[test]
fn held_is_visible_in_existing_drift_output() {
    let f = Fixture::new();
    f.target("src/plangraph/keeper/daemon.py", "VALUE = 2\n");
    let (code, line) = f.run();
    assert_eq!(code, 3);
    assert_eq!(f.head(), f.loaded);
    assert!(drift(&f, false).contains(&line));
    let json: serde_json::Value = serde_json::from_str(&drift(&f, true)).unwrap();
    assert_eq!(json[0]["main_ff"], line);
    let path = advance::status_path(&System, &f.repo).unwrap();
    assert_eq!(
        fs::metadata(path).unwrap().permissions().mode() & 0o777,
        0o600
    );
}
#[test]
fn absent_status_preserves_earlier_drift_shape() {
    let f = Fixture::new();
    let json: serde_json::Value = serde_json::from_str(&drift(&f, true)).unwrap();
    assert!(json[0].get("main_ff").is_none());
    assert!(!drift(&f, false).contains("HELD"));
}
#[test]
fn status_reader_uses_each_worktrees_git_dir() {
    let f = Fixture::new();
    let linked = f._temp.path().join("linked");
    git(
        &f.repo,
        &[
            "worktree",
            "add",
            "--detach",
            linked.to_str().unwrap(),
            &f.loaded,
        ],
    );
    let path = advance::status_path(&System, &linked).unwrap();
    assert_ne!(path, advance::status_path(&System, &f.repo).unwrap());
    fs::write(&path, "HELD fixture\n").unwrap();
    let result = Command::new(env!("CARGO_BIN_EXE_pg-mainff"))
        .args(["status", linked.to_str().unwrap()])
        .env("GIT_DIR", f.repo.join(".git"))
        .output()
        .unwrap();
    assert!(result.status.success());
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&result.stdout).unwrap(),
        "HELD fixture"
    );
}
#[test]
fn hourly_template_includes_guard_and_rust_path() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let service =
        fs::read_to_string(root.join("deploy/systemd/plangraph-main-ff.service")).unwrap();
    let timer = fs::read_to_string(root.join("deploy/systemd/plangraph-main-ff.timer")).unwrap();
    assert!(service.contains("Environment=PATH=%h/.cargo/bin:"));
    assert!(service.contains("-m plangraph.fleet_sync main-ff --repo ${PLANGRAPH_MAIN_TREE} --keeper-state ${PLANGRAPH_KEEPER_STATE}"));
    assert!(timer.contains("OnUnitActiveSec=1h\n"));
    assert!(timer.contains("Unit=plangraph-main-ff.service\n"));
}
