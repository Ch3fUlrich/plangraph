#![cfg(target_os = "linux")]

use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

const REGISTRY: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../docs/schemas/examples/hosts.example.json"
);

const UNIT: &str = "[Service]\nEnvironmentFile=%h/.config/p/a.env\nExecStart=%h/.local/bin/tool --pins ${PG_PINS}\n";

fn dry_run(home: &Path, repo: &Path, host: &str, family: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .args(["--install-dry-run", "--registry", REGISTRY, "--repo"])
        .arg(repo)
        .args([host, family])
        .output()
        .unwrap()
}

fn fixture() -> (tempfile::TempDir, tempfile::TempDir) {
    let repo = tempfile::tempdir().unwrap();
    fs::create_dir_all(repo.path().join("deploy/systemd")).unwrap();
    fs::write(repo.path().join("deploy/systemd/a.service"), UNIT).unwrap();
    let home = tempfile::tempdir().unwrap();
    let bin = home.path().join(".local/bin/tool");
    fs::create_dir_all(bin.parent().unwrap()).unwrap();
    pg_worker_core::executable_test_support::write_executable(&bin, "");
    fs::create_dir_all(home.path().join(".config/p")).unwrap();
    (home, repo)
}

#[test]
fn a_host_missing_an_env_key_prints_it_and_exits_one() {
    let (home, repo) = fixture();
    fs::write(home.path().join(".config/p/a.env"), "OTHER=1\n").unwrap();
    let out = dry_run(home.path(), repo.path(), "code", "a.service");
    assert_eq!(out.status.code(), Some(1), "{out:?}");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        "MISSING env PG_PINS\n"
    );
}

#[test]
fn a_complete_host_exits_zero_with_no_output() {
    let (home, repo) = fixture();
    fs::write(
        home.path().join(".config/p/a.env"),
        "PG_PINS=/etc/hostname\n",
    )
    .unwrap();
    let out = dry_run(home.path(), repo.path(), "code", "a.service");
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    assert!(out.stdout.is_empty() && out.stderr.is_empty(), "{out:?}");
    // The registry resolves an alias to the same host.
    assert_eq!(
        dry_run(home.path(), repo.path(), "DevBox", "a.service")
            .status
            .code(),
        Some(0)
    );
}

#[test]
fn an_unknown_host_or_family_refuses_with_exit_two() {
    let (home, repo) = fixture();
    let out = dry_run(home.path(), repo.path(), "nowhere", "a.service");
    assert_eq!(out.status.code(), Some(2), "{out:?}");
    assert!(out.stdout.is_empty());
    let out = dry_run(home.path(), repo.path(), "code", "../a.service");
    assert_eq!(out.status.code(), Some(2), "{out:?}");
    // A host the registry holds but that is not Linux has no dry run.
    let out = dry_run(home.path(), repo.path(), "workstation", "a.service");
    assert_eq!(out.status.code(), Some(2), "{out:?}");
}

#[test]
fn the_here_mode_checks_a_needs_record_from_stdin() {
    use std::io::Write;
    let home = tempfile::tempdir().unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home.path())
        .arg("--install-dry-run-here")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let needs = r#"{"absent":[],"binaries":["%h/bin/x"],"env_files":[],"environment":{},"keys":[],"dirs":[],"built":null}"#;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(needs.as_bytes())
        .unwrap();
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(
        String::from_utf8_lossy(&out.stdout),
        "MISSING binary ~/bin/x\n"
    );
}
