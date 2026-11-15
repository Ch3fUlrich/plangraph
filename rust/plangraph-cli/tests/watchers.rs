use std::process::Command;

fn cli(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_plangraph"))
        .args(args)
        .output()
        .unwrap()
}

#[test]
fn resume_digest_keeps_pending_and_completed_watcher_outcomes() {
    let job = tempfile::tempdir().unwrap();
    let watchers = job.path().join("watchers");
    std::fs::create_dir_all(watchers.join("push")).unwrap();
    std::fs::write(
        watchers.join("push/request.toml"),
        "argv = ['false']\ncwd = '/'\n",
    )
    .unwrap();
    let args = ["resume-digest", job.path().to_str().unwrap()];
    let pending = cli(&args);
    assert!(
        pending.status.success(),
        "{}",
        String::from_utf8_lossy(&pending.stderr)
    );
    assert!(String::from_utf8_lossy(&pending.stdout).contains("push: pending"));
    let run = cli(&["watcher-run", watchers.join("push").to_str().unwrap()]);
    assert!(!run.status.success());
    for _ in 0..2 {
        let digest = cli(&args);
        assert!(digest.status.success());
        assert!(String::from_utf8_lossy(&digest.stdout).contains("push: exit 1"));
    }
}

#[test]
fn watcher_records_spawn_errors_and_refuses_reexecution() {
    let job = tempfile::tempdir().unwrap();
    std::fs::write(
        job.path().join("request.toml"),
        "argv = ['/no/such/watcher-command']\ncwd = '/'\n",
    )
    .unwrap();
    let args = ["watcher-run", job.path().to_str().unwrap()];
    assert!(!cli(&args).status.success());
    let result = std::fs::read(job.path().join("result.toml")).unwrap();
    assert!(String::from_utf8_lossy(&result).contains("error"));
    assert!(!cli(&args).status.success());
    assert_eq!(
        std::fs::read(job.path().join("result.toml")).unwrap(),
        result
    );
}

// Opt-in host integration test: requires a running systemd user manager.
#[cfg(all(unix, feature = "systemd-tests"))]
#[test]
fn killed_orchestrator_keeps_detached_watcher_result() {
    use std::io::{Read, Write};
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::os::unix::process::CommandExt;
    use std::time::{Duration, Instant};
    const TEST: &str = "killed_orchestrator_keeps_detached_watcher_result";
    match std::env::var("PG_WATCHER_TEST_ROLE").as_deref() {
        Ok("workload") => {
            let mut stream =
                UnixStream::connect(std::env::var("PG_WATCHER_TEST_SOCKET").unwrap()).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(10)))
                .unwrap();
            stream.write_all(b"ready").unwrap();
            let mut go = [0; 1];
            stream.read_exact(&mut go).unwrap();
            assert_eq!(go, [1]);
            println!("authorized continuation completed");
            return;
        }
        Ok("orchestrator") => {
            let output = cli(&[
                "watcher-launch",
                &std::env::var("PG_WATCHER_TEST_JOB").unwrap(),
                "push",
                "--",
                "/usr/bin/env",
                "PG_WATCHER_TEST_ROLE=workload",
                &format!(
                    "PG_WATCHER_TEST_SOCKET={}",
                    std::env::var("PG_WATCHER_TEST_SOCKET").unwrap()
                ),
                std::env::current_exe().unwrap().to_str().unwrap(),
                "--exact",
                TEST,
                "--nocapture",
            ]);
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            // Parent kills this session after the service signals readiness.
            loop {
                std::thread::park();
            }
        }
        _ => (),
    }
    let job = tempfile::tempdir().unwrap();
    let socket = job.path().join("ready.sock");
    let listener = UnixListener::bind(&socket).unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut orchestrator = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", TEST, "--nocapture"])
        .env("PG_WATCHER_TEST_ROLE", "orchestrator")
        .env("PG_WATCHER_TEST_JOB", job.path())
        .env("PG_WATCHER_TEST_SOCKET", &socket)
        .process_group(0)
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut stream = loop {
        match listener.accept() {
            Ok((stream, _)) => break stream,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(
                    orchestrator.try_wait().unwrap().is_none(),
                    "service admission failed"
                );
                assert!(Instant::now() < deadline, "service did not become ready");
                std::thread::yield_now();
            }
            Err(error) => panic!("{error}"),
        }
    };
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .unwrap();
    let mut ready = [0; 5];
    stream.read_exact(&mut ready).unwrap();
    assert_eq!(&ready, b"ready");
    // SAFETY: the child owns this separate process group; our test is outside it.
    assert_eq!(
        unsafe { libc::kill(-(orchestrator.id() as i32), libc::SIGKILL) },
        0
    );
    assert!(!orchestrator.wait().unwrap().success());
    let args = ["resume-digest", job.path().to_str().unwrap()];
    assert!(String::from_utf8_lossy(&cli(&args).stdout).contains("push: pending"));
    stream.write_all(&[1]).unwrap();
    let result = job.path().join("watchers/push/result.toml");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !result.exists() {
        assert!(Instant::now() < deadline, "detached watcher outcome lost");
        std::thread::yield_now();
    }
    assert!(String::from_utf8_lossy(&cli(&args).stdout).contains("push: exit 0"));
    assert!(
        std::fs::read_to_string(job.path().join("watchers/push/stdout.log"))
            .unwrap()
            .contains("authorized continuation completed")
    );
    let duplicate = cli(&[
        "watcher-launch",
        job.path().to_str().unwrap(),
        "push",
        "--",
        "/bin/true",
    ]);
    assert!(!duplicate.status.success());
}

#[test]
fn interrupted_admission_does_not_hide_other_watcher_results() {
    let job = tempfile::tempdir().unwrap();
    let root = job.path().join("watchers");
    std::fs::create_dir_all(root.join("interrupted")).unwrap();
    std::fs::create_dir(root.join("completed")).unwrap();
    std::fs::write(root.join("completed/result.toml"), "code = 0\n").unwrap();
    let output = cli(&["resume-digest", job.path().to_str().unwrap()]);
    assert!(output.status.success());
    let text = String::from_utf8_lossy(&output.stdout);
    assert!(text.contains("completed: exit 0"));
    assert!(text.contains("interrupted: record error:"));
}
