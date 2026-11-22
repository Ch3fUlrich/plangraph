//! The real command line against a fake `docker`: the commands that would reach
//! Postgres are built, run and checked end to end, and no password leaks.

use pg_keeper_backup::names;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Output};

/// A fixed time for dump names built in the tests.
const DAY: u64 = 1_794_000_000;
const SECRET: &str = "s3ntinel-pw";

fn run(args: &[&str], log: &Path, collide: bool) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-keeper-backup"));
    cmd.args(args)
        .args(["--docker", env!("CARGO_BIN_EXE_fake-docker")])
        .env("PGPASSWORD", SECRET)
        .env("PGHOST", "db.example")
        .env("FAKE_DOCKER_LOG", log);
    if collide {
        cmd.env("FAKE_DOCKER_COLLIDE", "1");
    }
    cmd.output().unwrap()
}

fn text(out: &Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

#[test]
fn dump_verify_runs_the_postgres_commands_and_prints_no_password() {
    let tmp = tempfile::tempdir().unwrap();
    let (dir, log) = (tmp.path().join("backups"), tmp.path().join("docker.log"));
    let out = run(
        &["dump", "--verify", "--dir", dir.to_str().unwrap()],
        &log,
        false,
    );
    assert!(out.status.success(), "{}", text(&out));
    assert!(!text(&out).contains(SECRET));
    let calls = std::fs::read_to_string(&log).unwrap();
    assert!(!calls.contains(SECRET), "{calls}");
    for want in [
        "--log-driver none",
        "-e PGPASSWORD ",
        "pg_dump -Fc --no-owner -n keeper",
        "pg_restore -l /backup/.keeper-schema-seq19200-",
        "--data-only --schema keeper --table events --file - /backup/",
        "CREATE DATABASE \"pgkb_scratch_19200_",
        "pg_restore --no-owner --no-privileges --exit-on-error -d pgkb_scratch_19200_",
        "DROP DATABASE IF EXISTS \"pgkb_scratch_19200_",
    ] {
        assert!(calls.contains(want), "missing {want:?} in\n{calls}");
    }
    let dumps: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(dumps.len(), 2, "{dumps:?}");
    for path in dumps {
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
}

#[test]
fn a_database_name_collision_is_reported_without_the_password_and_without_a_drop() {
    let tmp = tempfile::tempdir().unwrap();
    let (dir, log) = (tmp.path().join("backups"), tmp.path().join("docker.log"));
    let out = run(
        &["dump", "--verify", "--dir", dir.to_str().unwrap()],
        &log,
        true,
    );
    assert!(!out.status.success());
    let shown = text(&out);
    assert!(
        shown.contains("already exists") && shown.contains("***"),
        "{shown}"
    );
    assert!(!shown.contains(SECRET), "{shown}");
    assert!(!std::fs::read_to_string(&log)
        .unwrap()
        .contains("DROP DATABASE"));
}

#[test]
fn restore_test_refuses_an_old_dump_without_calling_docker() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("docker.log");
    let old = tmp.path().join(names::dump_name(19095, DAY));
    std::fs::write(&old, "x").unwrap();
    let out = run(
        &["restore-test", "--file", old.to_str().unwrap()],
        &log,
        false,
    );
    assert!(!out.status.success());
    assert!(text(&out).contains("older than 19096"));
    assert!(!log.exists());
}

#[test]
fn restore_test_rejects_a_file_that_is_not_an_archive() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("docker.log");
    let junk = tmp.path().join(names::dump_name(19200, DAY));
    std::fs::write(&junk, "not an archive").unwrap();
    let out = run(
        &["restore-test", "--file", junk.to_str().unwrap()],
        &log,
        false,
    );
    assert!(!out.status.success());
    assert!(
        text(&out).contains("valid archive") && !text(&out).contains(SECRET),
        "{}",
        text(&out)
    );
    let good = tmp.path().join(names::dump_name(19200, DAY + 86_400));
    std::fs::write(&good, "PGDMP seq=19200").unwrap();
    let ok = run(
        &["restore-test", "--file", good.to_str().unwrap()],
        &log,
        false,
    );
    assert!(ok.status.success(), "{}", text(&ok));
}

#[test]
fn restore_test_rejects_an_archive_whose_content_is_older_than_its_name() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("docker.log");
    let renamed = tmp.path().join(names::dump_name(19200, DAY + 86_400));
    std::fs::write(&renamed, "PGDMP seq=18000").unwrap();
    let out = run(
        &["restore-test", "--file", renamed.to_str().unwrap()],
        &log,
        false,
    );
    assert!(!out.status.success());
    assert!(
        text(&out).contains("below the dump name's seq 19200"),
        "{}",
        text(&out)
    );
    let calls = std::fs::read_to_string(&log).unwrap();
    assert!(!calls.contains("CREATE DATABASE"), "{calls}");
    assert!(!calls.contains("-d pgkb_scratch_"), "{calls}");
}
