use super::{
    group::*,
    group_windows::{creation_time, IsProcessInJob, SetLastError},
};
use std::{
    ffi::OsString,
    io::Read,
    os::windows::ffi::OsStrExt,
    process::Command,
    thread,
    time::{Duration, Instant},
};
use windows_sys::Win32::{
    Foundation::*,
    System::{JobObjects::*, Threading::*},
};

#[test]
fn holder_helper() {
    if std::env::args().any(|s| s == "--exact") {
        std::process::exit(super::job_holder_windows::helper());
    }
}

#[test]
fn fake_child() {
    let Ok(mode) = std::env::var("PG_FAKE_CHILD") else {
        return;
    };
    match mode.as_str() {
        "spawn-sleep" => {
            let mut child = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "run::group_windows_tests::fake_child",
                    "--nocapture",
                ])
                .env("PG_FAKE_CHILD", "sleep")
                .spawn()
                .unwrap();
            thread::sleep(Duration::from_secs(30));
            let _ = child.wait();
        }
        "sleep" => thread::sleep(Duration::from_secs(30)),
        "echo" => println!("{}", std::env::var("FOO").unwrap()),
        "exit3" => std::process::exit(3),
        "injob" => {
            let mut inside = 0;
            // SAFETY: pseudo-handle is valid and inside is writable; null asks about any job.
            assert_ne!(
                unsafe { IsProcessInJob(GetCurrentProcess(), std::ptr::null_mut(), &mut inside) },
                0
            );
            // Keep the child live until the parent has checked job membership.
            let mut input = String::new();
            std::io::stdin().read_to_string(&mut input).unwrap();
            println!("injob={inside}");
        }
        _ => panic!("unknown fake mode"),
    }
}
fn launch(
    unit: &str,
    mode: &str,
    missing: bool,
    piped: bool,
) -> Result<(Grouped, tempfile::TempDir), LaunchError> {
    launch_bounded(unit, mode, missing, piped, None)
}
fn launch_bounded(
    unit: &str,
    mode: &str,
    missing: bool,
    piped: bool,
    seconds: Option<u64>,
) -> Result<(Grouped, tempfile::TempDir), LaunchError> {
    let dir = tempfile::tempdir().unwrap();
    let program = if missing {
        dir.path().join("missing.exe")
    } else {
        std::env::current_exe().unwrap()
    };
    let args = [
        "--exact".into(),
        "run::group_windows_tests::fake_child".into(),
        "--nocapture".into(),
    ];
    let env: Vec<(OsString, OsString)> = vec![
        ("PG_FAKE_CHILD".into(), mode.into()),
        ("FOO".into(), "expected-value".into()),
        ("SystemRoot".into(), std::env::var_os("SystemRoot").unwrap()),
        ("PATH".into(), std::env::var_os("PATH").unwrap_or_default()),
    ];
    // SAFETY: updates only this thread's last-error slot before launch.
    unsafe { SetLastError(ERROR_ALREADY_EXISTS) };
    let grouped = SystemLauncher.launch(
        unit,
        &LaunchSpec {
            program: program.as_os_str(),
            args: &args,
            cwd: dir.path(),
            env: &env,
            stdin_piped: piped,
        },
        &Confinement::default(),
        &RunLimits {
            windows_holder_max_s: seconds,
            memory_max: Some("1".into()),
            tasks_max: Some(1),
        },
    )?;
    Ok((grouped, dir))
}
fn named(unit: &str) -> bool {
    let wide: Vec<_> = std::ffi::OsStr::new(&job_name(unit))
        .encode_wide()
        .chain(Some(0))
        .collect();
    // SAFETY: terminated name and query-only access.
    let handle = unsafe { OpenJobObjectW(4, 0, wide.as_ptr()) };
    if handle.is_null() {
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(ERROR_FILE_NOT_FOUND as i32)
        );
        false
    } else {
        // SAFETY: the newly opened handle is closed exactly once.
        unsafe {
            CloseHandle(handle);
        }
        true
    }
}
fn until(mut predicate: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(2);
    while !predicate() {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(10));
    }
}
fn gone(pid: u32) -> bool {
    // SAFETY: numeric pid; RAII is simulated by immediate close after the query.
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if handle.is_null() {
        return true;
    }
    // SAFETY: open process handle, nonblocking wait, then exactly one close.
    unsafe {
        let done = WaitForSingleObject(handle, 0) == WAIT_OBJECT_0;
        CloseHandle(handle);
        done
    }
}
#[test]
fn name_lifetime_collision_and_drop() {
    let unit = "pg-win-lifetime";
    let (mut g, _dir) = launch(unit, "sleep", false, false).unwrap();
    assert_eq!(
        g.group.identity().group.as_deref(),
        Some(job_name(unit).as_str())
    );
    assert!(named(unit));
    assert!(g.group.members().unwrap().contains(&g.child.id()));
    assert_eq!(g.group.identity().start_time, creation_time(g.child.id()));
    assert!(g.group.identity().start_time.is_some());
    assert!(matches!(
        launch(unit, "sleep", false, false),
        Err(LaunchError::JobExists)
    ));
    let pid = g.child.id();
    g.group.kill();
    drop(g.group);
    until(|| gone(pid));
    g.child.wait().unwrap();
    until(|| !named(unit));
}
#[test]
fn descendants_and_stop() {
    let (mut g, _dir) = launch("pg-win-tree", "spawn-sleep", false, false).unwrap();
    until(|| g.group.members().unwrap().len() == 2);
    let pids = g.group.members().unwrap();
    assert!(pids.contains(&g.child.id()));
    assert_eq!(
        stop_group(&*g.group, Duration::ZERO, Duration::from_secs(2)),
        StopOutcome::Empty
    );
    assert!(pids.iter().all(|&pid| gone(pid)));
    g.child.wait().unwrap();
}
#[test]
fn pipes_exit_and_assignment() {
    for (mode, expected) in [
        ("echo", "expected-value"),
        ("injob", "injob=1"),
        ("exit3", ""),
    ] {
        let (mut g, _dir) = launch("pg-win-pipes", mode, false, true).unwrap();
        if mode == "injob" {
            assert!(g.group.members().unwrap().contains(&g.child.id()));
        }
        assert!(_dir.path().exists());
        assert!(g.child.stdin.is_some());
        if mode == "injob" {
            drop(g.child.stdin.take());
        }
        assert!(g.child.stderr.is_some());
        let mut text = String::new();
        g.child
            .stdout
            .take()
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        assert!(text.contains(expected));
        assert_eq!(
            g.child.wait().unwrap().code(),
            Some(if mode == "exit3" { 3 } else { 0 })
        );
    }
}
#[test]
fn invalid_and_missing_program() {
    assert!(matches!(
        launch("a.b", "sleep", true, false),
        Err(LaunchError::BadUnit)
    ));
    assert!(matches!(
        launch("pg-win-missing", "sleep", true, false),
        Err(LaunchError::Io(_))
    ));
    assert!(!named("pg-win-missing"));
}

#[test]
fn daemon_close_survives_then_fixed_holder_expiry_kills() {
    let (mut g, _dir) =
        launch_bounded("pg-win-holder-expiry", "sleep", false, false, Some(1)).unwrap();
    let pid = g.child.id();
    assert!(g.group.holder_deadline_ms().is_some());
    drop(g.group);
    assert!(!gone(pid), "closing daemon handle must not kill payload");
    until(|| gone(pid));
    g.child.wait().unwrap();
    until(|| !named("pg-win-holder-expiry"));
}

#[test]
fn holder_launch_failure_is_closed() {
    super::job_holder_windows::FAIL_LAUNCH.set(true);
    let result = launch("pg-win-holder-fail", "sleep", false, false);
    super::job_holder_windows::FAIL_LAUNCH.set(false);
    assert!(matches!(result, Err(LaunchError::JobUnavailable)));
    assert!(!named("pg-win-holder-fail"));
}
