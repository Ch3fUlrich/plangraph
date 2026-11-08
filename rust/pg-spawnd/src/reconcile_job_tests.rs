use super::*;
#[test]
fn exact_job_binding_only() {
    assert!(scope("run-test", "Local\\pg-run-test").is_ok());
    for name in [
        "Global\\pg-run-test",
        "Local\\pg-other",
        "Local\\pg-../run-test",
    ] {
        assert!(scope("run-test", name).is_err());
    }
    assert!(scope("../escape", "Local\\pg-../escape").is_err());
}
#[test]
fn absent_start_and_groupless_records_refused() {
    for group in [None, Some("Local\\pg-run-test".to_string())] {
        assert!(validate(
            "run-test",
            &Proc {
                pid: 42,
                start_time: None,
                group,
                pgid: None
            }
        )
        .is_err());
    }
}
#[test]
fn reconcile_job_zero_identity_refused() {
    for (pid, start_time) in [(0, Some(99)), (42, Some(0)), (42, None)] {
        assert!(validate(
            "run-test",
            &Proc {
                pid,
                start_time,
                group: Some("Local\\pg-run-test".into()),
                pgid: None,
            }
        )
        .is_err());
    }
    assert!(validate(
        "run-test",
        &Proc {
            pid: 42,
            start_time: Some(99),
            group: Some("Local\\pg-run-test".into()),
            pgid: None,
        }
    )
    .is_ok());
}

struct JobFake {
    gone: bool,
    active: io::Result<bool>,
    terminated: bool,
}
impl OrphanJob for JobFake {
    fn gone(&mut self) -> io::Result<bool> {
        Ok(self.gone)
    }
    fn empty(&mut self) -> io::Result<bool> {
        self.active
            .as_ref()
            .map(|v| !v)
            .map_err(|e| e.kind().into())
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.terminated = true;
        Ok(())
    }
}
#[test]
fn gone_nonempty_or_unreadable_job_is_unknown() {
    for active in [Ok(true), Err(io::ErrorKind::PermissionDenied.into())] {
        let mut job = JobFake {
            gone: true,
            active,
            terminated: false,
        };
        assert!(orphan_empty(&mut job).is_err());
        assert!(!job.terminated);
    }
}
#[test]
fn orphan_cleanup_uses_verified_handle() {
    let mut job = JobFake {
        gone: true,
        active: Ok(false),
        terminated: false,
    };
    assert!(orphan_empty(&mut job).unwrap());
    assert!(job.terminated);
}
#[test]
fn gone_rejects_foreign_run_before_open() {
    let identity = Proc {
        pid: 42,
        start_time: Some(99),
        group: Some("Local\\pg-run-other".into()),
        pgid: None,
    };
    assert!(matches!(
        crate::reconcile_store::gone("run-test", Some(&identity)),
        Err(crate::reconcile::ReconcileError::Unknown)
    ));
}

#[test]
fn live_job_probe_and_cleanup_never_terminate() {
    let mut job = JobFake {
        gone: false,
        active: Ok(true),
        terminated: false,
    };
    assert!(!probe(&mut job).unwrap());
    assert!(!orphan_empty(&mut job).unwrap());
    assert!(!job.terminated);
}
#[test]
fn pure_gone_probe_never_terminates() {
    let mut job = JobFake {
        gone: true,
        active: Ok(false),
        terminated: false,
    };
    assert!(probe(&mut job).unwrap());
    assert!(!job.terminated);
    assert!(scope("run-test", "Local\\pg-run-other").is_err());
}
