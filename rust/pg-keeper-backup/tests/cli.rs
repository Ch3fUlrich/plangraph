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
    run_env(args, log, collide, &[])
}

fn run_env(args: &[&str], log: &Path, collide: bool, env: &[(&str, &str)]) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_pg-keeper-backup"));
    cmd.args(args)
        .args(["--docker", env!("CARGO_BIN_EXE_fake-docker")])
        .env("PGPASSWORD", SECRET)
        .env("PGHOST", "db.example")
        .env("FAKE_DOCKER_LOG", log);
    if collide {
        cmd.env("FAKE_DOCKER_COLLIDE", "1");
    }
    cmd.envs(env.iter().copied());
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

#[test]
fn dump_verify_follows_the_live_table_count_and_fails_on_a_different_listing() {
    let tmp = tempfile::tempdir().unwrap();
    let (dir, log) = (tmp.path().join("backups"), tmp.path().join("docker.log"));
    let args = ["dump", "--verify", "--dir", dir.to_str().unwrap()];
    let ok = run_env(&args, &log, false, &[("FAKE_DOCKER_TABLES", "65")]);
    assert!(ok.status.success(), "{}", text(&ok));
    assert!(text(&ok).contains("verified=yes"), "{}", text(&ok));
    // the live read is one SELECT of max(seq) and the table count, nothing is written
    let calls = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        calls.matches("information_schema.tables").count(),
        1,
        "{calls}"
    );
    assert!(
        !calls.contains("INSERT") && !calls.contains("UPDATE"),
        "{calls}"
    );

    let bad_dir = tmp.path().join("bad");
    let bad_args = ["dump", "--verify", "--dir", bad_dir.to_str().unwrap()];
    let bad = run_env(
        &bad_args,
        &log,
        false,
        &[
            ("FAKE_DOCKER_TABLES", "65"),
            ("FAKE_DOCKER_ARCHIVE_TABLES", "43"),
        ],
    );
    assert!(!bad.status.success());
    assert!(
        text(&bad).contains("listing has 43 tables, expected 65"),
        "{}",
        text(&bad)
    );
}

#[test]
fn restore_test_defaults_to_the_live_count_and_tables_overrides_it() {
    let tmp = tempfile::tempdir().unwrap();
    let log = tmp.path().join("docker.log");
    let file = tmp.path().join(names::dump_name(19200, DAY));
    std::fs::write(&file, "PGDMP seq=19200").unwrap();
    let f = file.to_str().unwrap();
    let env = [
        ("FAKE_DOCKER_TABLES", "65"),
        ("FAKE_DOCKER_ARCHIVE_TABLES", "43"),
    ];
    let by_catalog = run_env(&["restore-test", "--file", f], &log, false, &env);
    assert!(!by_catalog.status.success());
    assert!(
        text(&by_catalog).contains("expected 65"),
        "{}",
        text(&by_catalog)
    );
    let forced = run_env(
        &["restore-test", "--file", f, "--tables", "43"],
        &log,
        false,
        &env,
    );
    assert!(forced.status.success(), "{}", text(&forced));
    assert!(text(&forced).contains("tables 43"), "{}", text(&forced));
}
