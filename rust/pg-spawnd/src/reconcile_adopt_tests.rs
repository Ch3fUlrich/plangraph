use super::*;
#[test]
fn multithreaded_signal_child() {
    if std::env::var_os("PG_TEST_THREADED_CHILD").is_none() {
        return;
    }
    let threads: Vec<_> = (0..3)
        .map(|_| {
            std::thread::spawn(|| loop {
                std::thread::park();
            })
        })
        .collect();
    println!("READY");
    for thread in threads {
        thread.join().unwrap();
    }
}

#[test]
fn scope_signals_multithreaded_process_via_process_members_only() {
    use std::{io::BufRead, os::unix::process::ExitStatusExt, process::Command};
    struct ChildGuard(std::process::Child);
    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
    let root = std::env::temp_dir().join(format!("pg-thread-signal-{}", std::process::id()));
    let path = root.clone();
    std::fs::create_dir_all(&path).unwrap();
    std::fs::write(path.join("cgroup.events"), "populated 1\n").unwrap();
    struct RootGuard;
    impl Drop for RootGuard {
        fn drop(&mut self) {
            TEST_ROOT.with(|root| *root.borrow_mut() = None);
        }
    }
    TEST_ROOT.with(|value| *value.borrow_mut() = Some(root.clone()));
    let _guard = RootGuard;
    for force in [false, true] {
        let mut child = ChildGuard(
            Command::new(std::env::current_exe().unwrap())
                .args(["multithreaded_signal_child", "--nocapture"])
                .env("PG_TEST_THREADED_CHILD", "1")
                .stdout(std::process::Stdio::piped())
                .spawn()
                .unwrap(),
        );
        let output = std::io::BufReader::new(child.0.stdout.take().unwrap());
        assert!(output.lines().any(|line| line.unwrap().contains("READY")));
        let pid = child.0.id();
        let tids: Vec<_> = std::fs::read_dir(format!("/proc/{pid}/task"))
            .unwrap()
            .map(|entry| entry.unwrap().file_name().into_string().unwrap())
            .collect();
        assert!(tids.len() > 1);
        std::fs::write(path.join("cgroup.procs"), pid.to_string()).unwrap();
        std::fs::write(path.join("cgroup.threads"), tids.join("\n")).unwrap();
        let mut group = Adopted::new(Scope(path.clone()));
        if force {
            group.kill().unwrap();
        } else {
            group.terminate().unwrap();
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
        let status = loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                break status;
            }
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(10));
        };
        assert_eq!(
            status.signal(),
            Some(if force { libc::SIGKILL } else { libc::SIGTERM })
        );
    }
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn cpu_reads_scope_usage_then_member_ticks_and_handles_unreadable() {
    let path = std::env::temp_dir().join(format!("pg-cpu-source-{}", std::process::id()));
    std::fs::create_dir(&path).unwrap();
    std::fs::write(path.join("cpu.stat"), "usage_usec 123000\n").unwrap();
    assert_eq!(cpu(&path).unwrap().total_ms, 123);
    std::fs::remove_file(path.join("cpu.stat")).unwrap();
    std::fs::write(path.join("cgroup.procs"), std::process::id().to_string()).unwrap();
    assert!(cpu(&path).is_ok());
    std::fs::remove_file(path.join("cgroup.procs")).unwrap();
    assert!(cpu(&path).is_err());
    std::fs::remove_dir(path).unwrap();
}
#[test]
fn gone_scope_ignores_live_reused_leader() {
    let run = "run-adopttest";
    let path = format!(
        "/sys/fs/cgroup/user.slice/user-{}.slice/pg-{run}.scope",
        unsafe { libc::geteuid() }
    );
    let identity = Proc {
        pid: std::process::id(),
        start_time: Some(1),
        pgid: None,
        group: Some(path),
    };
    assert!(crate::reconcile_store::gone(run, Some(&identity)).unwrap());
}
#[test]
fn foreign_and_traversal_scopes_refused() {
    assert!(scope("run-test", "/tmp/pg-run-test.scope").is_err());
    assert!(scope("../run-test", "/sys/fs/cgroup/pg-run-test.scope").is_err());
}
#[test]
fn vanished_member_does_not_abort_cpu_and_thread_only_scope_refuses_silent_signal() {
    let path = std::env::temp_dir().join(format!("pg-member-source-{}", std::process::id()));
    std::fs::create_dir(&path).unwrap();
    std::fs::write(
        path.join("cgroup.procs"),
        format!("{}\n2147483647\n", std::process::id()),
    )
    .unwrap();
    assert!(cpu(&path).is_ok());
    std::fs::write(path.join("cgroup.procs"), "").unwrap();
    std::fs::write(path.join("cgroup.threads"), "2147483647\n").unwrap();
    let mut pids = Vec::new();
    members(&path, &mut pids).unwrap();
    assert!(pids.is_empty());
    std::fs::write(path.join("cgroup.threads"), "").unwrap();
    std::fs::write(path.join("cgroup.events"), "populated 1\n").unwrap();
    assert!(Scope(path.clone()).signal(false).is_err());
    for name in ["cgroup.procs", "cgroup.threads", "cgroup.events"] {
        std::fs::remove_file(path.join(name)).unwrap();
    }
    std::fs::remove_dir(path).unwrap();
}
#[test]
fn cpu_requires_real_delta_and_errors_reset_baseline() {
    let mut sampler = pg_worker_core::cpu::Sampler::new(100);
    assert!(sampler
        .poll(0, &mut || Ok(CpuSample { total_ms: 10 }))
        .is_none());
    assert!(sampler
        .poll(30_000, &mut || Ok(CpuSample { total_ms: 110 }))
        .is_some());
    assert!(sampler.poll(60_000, &mut || Err(unknown())).is_none());
    assert!(sampler
        .poll(90_000, &mut || Ok(CpuSample { total_ms: 500 }))
        .is_none());
    assert!(sampler
        .poll(120_000, &mut || Ok(CpuSample { total_ms: 500 }))
        .is_none());
    assert!(sampler
        .poll(150_000, &mut || Ok(CpuSample { total_ms: 1 }))
        .is_none());
    assert!(sampler
        .poll(180_000, &mut || Ok(CpuSample { total_ms: 101 }))
        .is_some());
}
