use super::*;
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
    assert!(crate::reconcile_store::gone(Some(&identity)).unwrap());
}
#[test]
fn foreign_and_traversal_scopes_refused() {
    assert!(scope("run-test", "/tmp/pg-run-test.scope").is_err());
    assert!(scope("../run-test", "/sys/fs/cgroup/pg-run-test.scope").is_err());
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
