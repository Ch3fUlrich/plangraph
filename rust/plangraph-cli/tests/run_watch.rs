use std::{
    fs,
    io::{Read, Write},
    process::{Command, Stdio},
};

fn tool() -> Command {
    Command::new(env!("CARGO_BIN_EXE_pg-run-watch"))
}

#[test]
fn child_fixture() {
    if std::env::var_os("PG_WATCH_FIXTURE").is_none() {
        return;
    }
    let bytes: Vec<u8> = std::iter::once(0xfeffu16)
        .chain("Codex finished\r\nEXIT 7\r\n".encode_utf16())
        .flat_map(u16::to_le_bytes)
        .collect();
    std::io::stdout().write_all(&bytes).unwrap();
    std::io::stdout().flush().unwrap();
    let mut release = [0];
    std::io::stdin().read_exact(&mut release).unwrap();
    std::process::exit(7);
}

#[test]
fn utf16_finished_and_unfinished_runs_end_to_end() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("run with spaces.log");
    let mut launcher = tool()
        .arg("launch")
        .arg(&log)
        .arg("--")
        .arg(std::env::current_exe().unwrap())
        .args(["--exact", "child_fixture", "--nocapture"])
        .env("PG_WATCH_FIXTURE", "1")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    // The launcher acknowledges the new log and child before this probe.
    let mut ready = std::io::BufReader::new(launcher.stdout.take().unwrap());
    let mut line = String::new();
    std::io::BufRead::read_line(&mut ready, &mut line).unwrap();
    assert_eq!(line, "STARTED\n");
    let running = tool().arg("check").arg(&log).output().unwrap();
    assert_eq!(running.status.code(), Some(1));
    assert_eq!(running.stdout, b"RUNNING\n");
    launcher.stdin.take().unwrap().write_all(b"x").unwrap();
    assert_eq!(launcher.wait().unwrap().code(), Some(7));
    let finished = tool().arg("check").arg(&log).output().unwrap();
    assert!(finished.status.success());
    assert_eq!(finished.stdout, b"EXIT 7\n");
    assert_eq!(
        fs::read(log.with_extension("log.exit")).unwrap(),
        b"EXIT 7\n"
    );
    assert!(fs::read(&log)
        .unwrap()
        .windows(2)
        .any(|w| w == [0xff, 0xfe]));
    // An old marker must never be mistaken for a new run.
    assert_eq!(
        tool()
            .arg("launch")
            .arg(&log)
            .args(["--", "unused"])
            .status()
            .unwrap()
            .code(),
        Some(2)
    );
}

#[test]
fn quiet_run_reports_at_sixty_minutes_and_activity_resets_it() {
    use std::time::{Duration, SystemTime};
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("quiet.log");
    let file = fs::File::create(&log).unwrap();
    file.set_modified(SystemTime::now() - Duration::from_secs(3601))
        .unwrap();
    let quiet = tool().arg("check").arg(&log).output().unwrap();
    assert_eq!(quiet.status.code(), Some(3));
    assert_eq!(quiet.stdout, b"NO_CHANGE 60 minutes\n");
    file.set_modified(SystemTime::now()).unwrap();
    let active = tool().arg("check").arg(&log).output().unwrap();
    assert_eq!(active.status.code(), Some(1));
    fs::write(log.with_extension("log.exit"), b"EXIT 0\n").unwrap();
    file.set_modified(SystemTime::now() - Duration::from_secs(7200))
        .unwrap();
    assert!(tool().arg("check").arg(&log).status().unwrap().success());
}

#[test]
fn missing_logs_and_malformed_markers_report_errors() {
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("run.log");
    assert_eq!(
        tool().arg("check").arg(&log).status().unwrap().code(),
        Some(2)
    );
    fs::write(&log, b"log").unwrap();
    for bad in [
        b"EXIT 0\ntrailing".as_slice(),
        b"\xff\xfeE\0",
        b"EXIT nope\n",
        b"EXIT 0",
    ] {
        fs::write(log.with_extension("log.exit"), bad).unwrap();
        assert_eq!(
            tool().arg("check").arg(&log).status().unwrap().code(),
            Some(2)
        );
    }
}

#[test]
fn log_growth_counts_as_activity_when_windows_mtime_is_unchanged() {
    use std::time::{Duration, SystemTime};
    let dir = tempfile::tempdir().unwrap();
    let log = dir.path().join("growing.log");
    let old = SystemTime::now() - Duration::from_secs(7200);
    fs::write(&log, b"initial").unwrap();
    fs::File::options()
        .write(true)
        .open(&log)
        .unwrap()
        .set_modified(old)
        .unwrap();
    assert_eq!(
        tool()
            .arg("check")
            .arg(&log)
            .output()
            .unwrap()
            .status
            .code(),
        Some(3)
    );
    // Windows can defer updating last-write time while a writer holds the file.
    fs::write(&log, b"initial plus new output").unwrap();
    fs::File::options()
        .write(true)
        .open(&log)
        .unwrap()
        .set_modified(old)
        .unwrap();
    let active = tool().arg("check").arg(&log).output().unwrap();
    assert_eq!(active.status.code(), Some(1));
    assert_eq!(active.stdout, b"RUNNING\n");
    assert_eq!(
        tool()
            .arg("check")
            .arg(&log)
            .output()
            .unwrap()
            .status
            .code(),
        Some(1)
    );
}

#[test]
fn relative_log_paths_work_for_scheduler_probes() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("run.log"), b"active").unwrap();
    let active = tool()
        .current_dir(dir.path())
        .args(["check", "run.log"])
        .output()
        .unwrap();
    assert_eq!(active.status.code(), Some(1), "{:?}", active);
    assert_eq!(active.stdout, b"RUNNING\n");
}
