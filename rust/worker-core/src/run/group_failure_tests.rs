use super::*;
use std::{fs, os::unix::fs::PermissionsExt, process::Command};

#[test]
fn launch_retries_text_busy() {
    let mut attempts = 0;
    let mut child = spawn_retrying(|| {
        attempts += 1;
        if attempts == 1 {
            Err(io::Error::from_raw_os_error(libc::ETXTBSY))
        } else {
            Command::new("sh").args(["-c", "exit 0"]).spawn()
        }
    })
    .unwrap();
    assert_eq!(attempts, 2);
    assert!(child.wait().unwrap().success());
}

#[test]
fn scope_probe_failure_and_timeout() {
    let dir = tempfile::tempdir().unwrap();
    let timeout = Duration::from_millis(50);
    assert!(probe_command(&mut Command::new(dir.path().join("missing")), timeout).is_err());
    let mut failure = Command::new("sh");
    failure.args(["-c", "exit 1"]);
    assert!(probe_command(&mut failure, timeout).is_err());
    let mut sleeper = Command::new("sh");
    sleeper.args(["-c", "sleep 30 & wait"]);
    let start = Instant::now();
    assert!(probe_command(&mut sleeper, timeout).is_err());
    assert!(start.elapsed() >= timeout);
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[test]
fn scope_entry_timeout() {
    let dir = tempfile::tempdir().unwrap();
    let mut grouped = plain("sleep 30", dir.path(), false);
    let roots = Roots {
        proc: dir.path().into(),
        cgroup: dir.path().into(),
    };
    let start = Instant::now();
    assert!(matches!(
        enter_scope(
            &mut grouped.child,
            "pg-test",
            &roots,
            Duration::from_millis(50)
        ),
        Err(LaunchError::ScopeNotEntered)
    ));
    assert!(grouped.child.try_wait().unwrap().is_some());
    assert!(start.elapsed() < Duration::from_secs(2));
}

#[test]
fn members_permission_denied() {
    // Root bypasses mode bits, so this assertion cannot be exercised as root.
    // SAFETY: geteuid has no arguments and only reads the effective uid.
    if unsafe { libc::geteuid() } == 0 {
        return;
    }
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("cgroup.procs");
    fs::write(&path, "").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).unwrap();
    let group = UnixGroup {
        identity: Identity {
            pid: 0,
            start_time: None,
            group: Some(dir.path().to_string_lossy().into_owned()),
        },
        roots: Roots::default(),
    };
    assert_eq!(
        group.members().unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
}

#[test]
fn kill_without_start_time() {
    let dir = tempfile::tempdir().unwrap();
    let mut child = plain("exec sleep 30", dir.path(), false);
    let group = UnixGroup {
        identity: Identity {
            pid: child.child.id(),
            start_time: None,
            group: None,
        },
        roots: Roots {
            proc: dir.path().join("unreadable"),
            cgroup: dir.path().into(),
        },
    };
    group.kill();
    let deadline = Instant::now() + Duration::from_secs(2);
    while child.child.try_wait().unwrap().is_none() {
        if Instant::now() >= deadline {
            child.child.kill().unwrap();
            child.child.wait().unwrap();
            panic!("group kill did not signal the child");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
