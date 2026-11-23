#![cfg(target_os = "linux")]

use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

fn install(home: &Path, family: &str) -> Output {
    Command::new(env!("CARGO_BIN_EXE_host-sync"))
        .env_clear()
        .env("HOME", home)
        .args(["--install-env", family])
        .output()
        .unwrap()
}

#[test]
fn disk_hygiene_environment_matches_unit_and_preserves_existing_configuration() {
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().join("fixture home");
    let env = home.join(".config/plangraph/disk-hygiene.env");
    let output = install(&home, "disk-hygiene");
    assert!(output.status.success(), "{output:?}");
    let text = fs::read_to_string(&env).unwrap();
    for (key, path) in [
        ("PG_REPO", "code/plangraph"),
        ("PG_WORKTREES", "code/plangraph/.claude/worktrees"),
        ("PG_PINS", ".config/plangraph/pins.json"),
    ] {
        assert!(
            text.contains(&format!("{key}=\"{}\"\n", home.join(path).display())),
            "{text}"
        );
    }
    let binary = home.join(".local/bin/pg-disk-hygiene");
    fs::create_dir_all(binary.parent().unwrap()).unwrap();
    std::os::unix::fs::symlink("/bin/true", binary).unwrap();
    let unit = include_str!("../../../deploy/systemd/pg-disk-hygiene.service");
    if Path::new("/usr/bin/systemd-analyze").is_file() {
        let service = temp.path().join("pg-disk-hygiene.service");
        let unit = unit.replace(
            "%h/.local/bin/pg-disk-hygiene",
            &format!("\"{}\"", home.join(".local/bin/pg-disk-hygiene").display()),
        );
        fs::write(&service, unit).unwrap();
        let verify = Command::new("/usr/bin/systemd-analyze")
            .arg("verify")
            .arg(service)
            .output()
            .unwrap();
        assert!(verify.status.success(), "{verify:?}");
    }
    fs::write(&env, "PG_REPO=existing\n").unwrap();
    assert!(install(&home, "disk-hygiene").status.success());
    assert_eq!(fs::read_to_string(env).unwrap(), "PG_REPO=existing\n");
}

#[test]
fn keeper_backup_installs_only_empty_connection_keys_with_private_mode() {
    use std::os::unix::fs::PermissionsExt;
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path();
    let output = install(home, "keeper-backup");
    assert!(output.status.success(), "{output:?}");
    let env = home.join(".config/plangraph/keeper-backup.env");
    let text = fs::read_to_string(&env).unwrap();
    let docker = include_str!("../../pg-keeper-backup/src/docker.rs");
    let keys = docker
        .split_once("pub const PG_ENV:")
        .unwrap()
        .1
        .split_once("= [")
        .unwrap()
        .1
        .split_once("];")
        .unwrap()
        .0;
    let expected: Vec<_> = keys
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.trim_end_matches(',').trim_matches('"'))
        .collect();
    let entries: Vec<_> = text
        .lines()
        .filter(|line| !line.starts_with('#'))
        .map(|line| line.split_once('=').unwrap())
        .collect();
    assert_eq!(
        entries,
        expected
            .into_iter()
            .map(|key| (key, ""))
            .collect::<Vec<_>>()
    );
    assert_eq!(
        fs::metadata(&env).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::write(&env, "# existing operator file\n").unwrap();
    assert!(install(home, "keeper-backup").status.success());
    assert_eq!(
        fs::read_to_string(env).unwrap(),
        "# existing operator file\n"
    );
}
