use super::*;
use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Command, Stdio};

/// Child-mode switch and lock path for the cross-process test.
const CHILD_ENV: &str = "PG_LOCKFILE_CHILD";
const CHILD_PATH_ENV: &str = "PG_LOCKFILE_CHILD_PATH";
/// The child runs exactly this test of its own binary.
const HELPER_TEST: &str = "lockfile::tests::child_lock_helper";
const MARKER_BUSY: &str = "PG_LOCKFILE_BUSY";
const MARKER_LOCKED: &str = "PG_LOCKFILE_LOCKED";

#[test]
fn try_lock_is_exclusive_and_idempotent() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("slots.lock");
    assert!(!path.exists());
    let first = try_lock(&path).unwrap().expect("first handle locks");
    assert!(path.exists(), "lock file created");
    assert!(
        try_lock(&path).unwrap().is_none(),
        "second handle is refused"
    );
    drop(first);
    assert!(
        try_lock(&path).unwrap().is_some(),
        "drop releases; re-creating the file is a no-op"
    );
}

#[test]
fn lock_with_retry_succeeds_after_holder_drops() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("slots.lock");
    let holder = try_lock(&path).unwrap().expect("holder locks");
    let other = path.clone();
    let worker = std::thread::spawn(move || {
        let mut rng = rand::rng();
        lock_with_retry(&other, Duration::from_secs(2), &mut rng).unwrap()
    });
    std::thread::sleep(Duration::from_millis(50));
    drop(holder);
    let guard = worker.join().unwrap();
    drop(guard);
    assert!(
        try_lock(&path).unwrap().is_some(),
        "guard drop released the lock"
    );
}

#[test]
fn lock_with_retry_times_out_when_never_dropped() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("slots.lock");
    let holder = try_lock(&path).unwrap().expect("holder locks");
    let mut rng = rand::rng();
    let err = lock_with_retry(&path, Duration::from_millis(80), &mut rng).unwrap_err();
    assert!(matches!(err, LockError::Timeout), "got {err:?}");
    drop(holder);
}

#[test]
fn lock_exclusive_across_processes() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("slots.lock");
    let guard = try_lock(&path).unwrap().expect("parent locks");
    let mut child = Command::new(std::env::current_exe().unwrap())
        .arg("--exact")
        .arg(HELPER_TEST)
        .env(CHILD_ENV, "1")
        .env(CHILD_PATH_ENV, &path)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut out = BufReader::new(child.stdout.take().unwrap());
    let mut stdin = child.stdin.take().unwrap();

    // While the parent holds the lock, the child process is refused.
    writeln!(stdin, "go").unwrap();
    stdin.flush().unwrap();
    assert_eq!(read_marked(&mut out), MARKER_BUSY);

    // After the parent drops the guard, the child gets the lock.
    drop(guard);
    writeln!(stdin, "go").unwrap();
    stdin.flush().unwrap();
    assert_eq!(read_marked(&mut out), MARKER_LOCKED);

    // EOF on stdin tells the child to release and exit.
    drop(stdin);
    let status = child.wait().unwrap();
    assert!(status.success());
    assert!(
        try_lock(&path).unwrap().is_some(),
        "child exit released the lock"
    );
}

/// Read lines until one of the protocol markers appears (the child's own test
/// harness also writes to its stdout).
fn read_marked(out: &mut BufReader<std::process::ChildStdout>) -> String {
    let mut line = String::new();
    loop {
        line.clear();
        let n = out.read_line(&mut line).unwrap();
        assert!(n > 0, "child exited before answering");
        let trimmed = line.trim();
        if trimmed == MARKER_BUSY || trimmed == MARKER_LOCKED {
            return trimmed.to_string();
        }
    }
}

/// Child side of `lock_exclusive_across_processes`: only acts when the parent
/// spawned it with `PG_LOCKFILE_CHILD`; otherwise a no-op pass.
#[test]
fn child_lock_helper() {
    let Ok(path) = std::env::var(CHILD_PATH_ENV) else {
        return;
    };
    if std::env::var_os(CHILD_ENV).is_none() {
        return;
    }
    let path = Path::new(&path);
    let mut line = String::new();
    loop {
        line.clear();
        if std::io::stdin().read_line(&mut line).unwrap_or(0) == 0 {
            return; // parent gone: nothing left to prove
        }
        match try_lock(path).unwrap() {
            None => write_marked(MARKER_BUSY),
            Some(guard) => {
                write_marked(MARKER_LOCKED);
                let mut sink = [0u8; 1];
                let _ = std::io::stdin().read(&mut sink); // hold until EOF
                drop(guard);
                return;
            }
        }
    }
}

fn write_marked(marker: &str) {
    let mut out = std::io::stdout();
    out.write_all(marker.as_bytes()).unwrap();
    out.write_all(b"\n").unwrap();
    out.flush().unwrap();
}
