use super::*;
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
fn cpu_weak_requires_real_delta_and_errors_reset_baseline() {
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
}
