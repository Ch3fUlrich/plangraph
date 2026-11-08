use super::{group::*, group_unix::*};
use std::{ffi::OsStr, path::Path, time::Duration};
use std::{
    io::{self, Read},
    time::Instant,
};

fn plain(script: &str, cwd: &Path, piped: bool) -> Grouped {
    SystemLauncher
        .launch(
            "pg-test",
            &LaunchSpec {
                program: OsStr::new("sh"),
                args: &["-c".into(), script.into()],
                cwd,
                env: &[("FOO".into(), "bar".into())],
                stdin_piped: piped,
            },
            &Confinement::Plain,
            &RunLimits::default(),
        )
        .unwrap()
}

#[cfg(target_os = "linux")]
#[test]
fn oom_score_inherited_by_first_descendant() {
    let dir = tempfile::tempdir().unwrap();
    let before = std::fs::read_to_string("/proc/self/oom_score_adj").unwrap();
    let mut launched = plain(
        "cat /proc/self/oom_score_adj; sh -c 'cat /proc/self/oom_score_adj'",
        dir.path(),
        false,
    );
    let child = &mut launched.child;
    let mut text = String::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_string(&mut text)
        .unwrap();
    assert!(child.wait().unwrap().success());
    let scores: Vec<i32> = text
        .split_whitespace()
        .map(|s| s.parse().unwrap())
        .collect();
    assert_eq!(scores.len(), 2);
    assert!(scores.iter().all(|score| *score >= 500));
    assert_eq!(
        std::fs::read_to_string("/proc/self/oom_score_adj").unwrap(),
        before
    );
}

#[test]
fn command_contract() {
    let env = [
        ("SECRET_TOKEN".into(), "private-value".into()),
        ("XDG_RUNTIME_DIR".into(), "/specified".into()),
    ];
    let args = ["a b".into(), "--flag".into()];
    let spec = LaunchSpec {
        program: OsStr::new("harness"),
        args: &args,
        cwd: Path::new("/"),
        env: &env,
        stdin_piped: true,
    };
    let c = scope_command(
        "pg-test",
        "agent-tests.slice",
        &RunLimits {
            windows_holder_max_s: None,
            memory_max: Some("1G".into()),
            tasks_max: Some(12),
        },
        &spec,
        &[
            ("XDG_RUNTIME_DIR".into(), "/ignored".into()),
            ("DBUS_SESSION_BUS_ADDRESS".into(), "bus".into()),
            ("HOME".into(), "/ignored".into()),
        ],
    );
    assert_eq!(c.get_program(), "systemd-run");
    let argv: Vec<_> = c.get_args().map(|s| s.to_str().unwrap()).collect();
    assert_eq!(
        argv,
        [
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--slice=agent-tests.slice",
            "--unit=pg-test",
            "-p",
            "MemoryMax=1G",
            "-p",
            "TasksMax=12",
            "--",
            "harness",
            "a b",
            "--flag"
        ]
    );
    assert!(!argv.contains(&"private-value"));
    let vars: std::collections::BTreeMap<_, _> = c.get_envs().collect();
    assert_eq!(vars.len(), 3);
    assert_eq!(
        vars[OsStr::new("XDG_RUNTIME_DIR")],
        Some(OsStr::new("/specified"))
    );
    assert_eq!(c.get_current_dir(), Some(Path::new("/")));
}

#[test]
fn environment_cwd_and_stdin() {
    let dir = tempfile::tempdir().unwrap();
    for piped in [false, true] {
        let mut g = plain(
            "printf '%s:%s\\n' \"$FOO\" \"${HOME-unset}\"; pwd; cat",
            dir.path(),
            piped,
        );
        assert_eq!(g.child.stdin.is_some(), piped);
        assert!(g.child.stderr.is_some());
        assert!(g.group.identity().start_time.is_some());
        drop(g.child.stdin.take());
        let mut text = String::new();
        g.child
            .stdout
            .take()
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        assert!(g.child.wait().unwrap().success());
        assert_eq!(text, format!("bar:unset\n{}\n", dir.path().display()));
    }
    let spec = LaunchSpec {
        program: OsStr::new("/nonexistent-program"),
        args: &[],
        cwd: dir.path(),
        env: &[],
        stdin_piped: false,
    };
    assert!(
        matches!(SystemLauncher.launch("pg-test", &spec, &Confinement::Plain, &RunLimits::default()), Err(LaunchError::Io(e)) if e.kind() == io::ErrorKind::NotFound)
    );
    assert!(matches!(
        SystemLauncher.launch("a.b", &spec, &Confinement::default(), &RunLimits::default()),
        Err(LaunchError::BadUnit)
    ));
}

#[test]
fn ignores_term_and_zero_grace() {
    let dir = tempfile::tempdir().unwrap();
    let mut g = plain(
        "trap '' TERM; echo ready; sleep 30 & wait",
        dir.path(),
        false,
    );
    use std::io::BufRead;
    let mut line = String::new();
    std::io::BufReader::new(g.child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let start = Instant::now();
    assert_eq!(
        stop_group(
            &*g.group,
            Duration::from_millis(150),
            Duration::from_secs(2)
        ),
        StopOutcome::Empty
    );
    assert!(start.elapsed() >= Duration::from_millis(150));
    assert!(start.elapsed() < Duration::from_secs(5));
    let _ = g.child.wait();
    let mut g = plain(
        "trap 'touch marker' TERM; echo ready; sleep 30 & wait",
        dir.path(),
        false,
    );
    line.clear();
    std::io::BufReader::new(g.child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(
        stop_group(&*g.group, Duration::ZERO, Duration::from_secs(2)),
        StopOutcome::Empty
    );
    let _ = g.child.wait();
    assert!(!dir.path().join("marker").exists());
}

#[test]
fn fake_cgroup() {
    let dir = tempfile::tempdir().unwrap();
    let mut child = plain("sleep 30", dir.path(), false);
    std::fs::write(
        dir.path().join("cgroup.procs"),
        format!("{}\n4294967295\ngarbage\n", child.child.id()),
    )
    .unwrap();
    std::fs::write(dir.path().join("cgroup.kill"), "").unwrap();
    let group = UnixGroup {
        identity: Identity {
            pid: child.child.id(),
            start_time: None,
            group: Some(dir.path().to_string_lossy().into_owned()),
        },
        roots: Roots::default(),
    };
    assert_eq!(group.members().unwrap(), vec![child.child.id()]);
    let nested = dir.path().join("child");
    std::fs::create_dir(&nested).unwrap();
    std::fs::write(nested.join("cgroup.procs"), child.child.id().to_string()).unwrap();
    std::fs::write(dir.path().join("cgroup.procs"), "").unwrap();
    assert_eq!(group.members().unwrap(), vec![child.child.id()]);
    std::fs::remove_file(nested.join("cgroup.procs")).unwrap();
    assert!(group.members().unwrap().is_empty());
    std::fs::create_dir(nested.join("cgroup.procs")).unwrap();
    assert!(group.members().is_err());
    std::fs::remove_dir(nested.join("cgroup.procs")).unwrap();
    std::fs::remove_dir(nested).unwrap();
    std::fs::write(
        dir.path().join("cgroup.procs"),
        child.child.id().to_string(),
    )
    .unwrap();
    group.kill();
    let _ = child.child.wait();
    assert_eq!(
        std::fs::read_to_string(dir.path().join("cgroup.kill")).unwrap(),
        "1"
    );
    std::fs::remove_file(dir.path().join("cgroup.procs")).unwrap();
    assert!(group.members().unwrap().is_empty());
    std::fs::create_dir(dir.path().join("cgroup.procs")).unwrap();
    assert!(group.members().is_err());
}

#[test]
fn live_scope() {
    let available = scope_available("agent-tests.slice");
    let start = Instant::now();
    assert_eq!(scope_available("agent-tests.slice"), available);
    assert!(start.elapsed() < Duration::from_millis(5));
    if !available {
        eprintln!("user manager unavailable; skipping live scope");
        return;
    }
    let unit = unit_name(&format!("test-{}", std::process::id()));
    let mut g = SystemLauncher
        .launch(
            &unit,
            &LaunchSpec {
                program: OsStr::new("sh"),
                args: &["-c".into(), "echo \"$FOO:${HOME-unset}\"; sleep 30".into()],
                cwd: Path::new("/"),
                env: &[("FOO".into(), "bar".into())],
                stdin_piped: false,
            },
            &Confinement::Scope {
                slice: "agent-tests.slice".into(),
            },
            &RunLimits::default(),
        )
        .unwrap();
    let path = g.group.identity().group.clone().unwrap();
    assert!(Path::new(&path).is_absolute());
    assert!(Path::new(&path).exists());
    assert!(path.ends_with(&format!("/{unit}.scope")));
    assert!(g.group.members().unwrap().contains(&g.child.id()));
    use std::io::BufRead;
    let mut line = String::new();
    std::io::BufReader::new(g.child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line, "bar:unset\n");
    assert_eq!(
        stop_group(
            &*g.group,
            Duration::from_millis(500),
            Duration::from_secs(2)
        ),
        StopOutcome::Empty
    );
    let _ = g.child.wait();
    let deadline = Instant::now() + Duration::from_secs(2);
    while Path::new(&path).exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(!Path::new(&path).exists());
}
#[test]
fn plain_stop() {
    let args = ["-c".into(), "sleep 30 & wait".into()];
    let spec = LaunchSpec {
        program: OsStr::new("sh"),
        args: &args,
        cwd: Path::new("/"),
        env: &[],
        stdin_piped: false,
    };
    let mut g = SystemLauncher
        .launch("pg-test", &spec, &Confinement::Plain, &RunLimits::default())
        .unwrap();
    std::thread::sleep(Duration::from_millis(80));
    assert!(g.group.members().unwrap().len() >= 2);
    assert_eq!(
        stop_group(
            &*g.group,
            Duration::from_millis(300),
            Duration::from_secs(2)
        ),
        StopOutcome::Empty
    );
    use std::os::unix::process::ExitStatusExt;
    assert!(g.child.wait().unwrap().signal().is_some());
}
#[test]
fn parsers() {
    assert_eq!(
        parse_cgroup_path(
            &format!("1:cpu:/pg-a.scope\n{CGROUP_V2_PREFIX}/user/pg-a.scope\n"),
            "pg-a"
        ),
        Some("/user/pg-a.scope".into())
    );
    assert_eq!(
        parse_cgroup_path(&format!("{CGROUP_V2_PREFIX}/pg-b.scope"), "pg-a"),
        None
    );
    assert_eq!(parse_cgroup_path("garbage", "pg-a"), None);
    let stat = format!("123 (a ) ( b) S 1 123 {} 42", vec!["0"; 16].join(" "));
    assert_eq!(parse_stat(&stat), Some(('S', 123, 42)));
    let pid = std::process::id();
    let start = proc_start_time(Path::new("/proc"), pid);
    assert!(start.is_some());
    assert_eq!(start, proc_start_time(Path::new("/proc"), pid));
    assert_eq!(proc_start_time(Path::new("/proc"), u32::MAX), None);
}

#[path = "group_failure_tests.rs"]
mod failures;
