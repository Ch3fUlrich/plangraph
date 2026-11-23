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
