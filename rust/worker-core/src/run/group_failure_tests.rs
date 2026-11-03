use super::*;
use std::{fs, os::unix::fs::PermissionsExt, process::Command, thread};

#[test]
fn launch_retries_text_busy() {
    use std::io::Write;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("busy.sh");
    let mut writer = fs::File::create(&path).unwrap();
    writer.write_all(b"#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    let closer = thread::spawn(move || {
        thread::sleep(Duration::from_millis(70));
        drop(writer);
    });
    let result = SystemLauncher.launch(
        "pg-test",
        &LaunchSpec {
            program: path.as_os_str(),
            args: &[],
            cwd: dir.path(),
            env: &[],
            stdin_piped: false,
        },
        &Confinement::Plain,
        &RunLimits::default(),
    );
    closer.join().unwrap();
    assert!(result.unwrap().child.wait().unwrap().success());
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
