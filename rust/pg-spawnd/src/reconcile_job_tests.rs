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
fn unknown_start_requires_scoped_group() {
    for group in [None, Some("Local\\pg-run-test".to_string())] {
        let expected = group.is_some();
        assert_eq!(
            validate(
                "run-test",
                &Proc {
                    pid: 42,
                    start_time: None,
                    group,
                    pgid: None
                }
            )
            .is_ok(),
            expected
        );
    }
}
#[test]
fn reconcile_job_zero_identity_refused() {
    for (pid, start_time) in [(0, Some(99)), (42, Some(0))] {
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
        assert!(matches!(
            map_probe(orphan_empty(&mut job)),
            Err(crate::reconcile::ReconcileError::Unknown)
        ));
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

#[test]
fn shared_mapping_distinguishes_absent_from_unverifiable() {
    assert!(matches!(
        map_open::<()>(Err(io::ErrorKind::NotFound.into())),
        Ok(None)
    ));
    assert!(matches!(
        map_open::<()>(Err(io::ErrorKind::PermissionDenied.into())),
        Err(crate::reconcile::ReconcileError::Unknown)
    ));
    for kind in [
        io::ErrorKind::NotFound,
        io::ErrorKind::WouldBlock,
        io::ErrorKind::PermissionDenied,
    ] {
        assert!(matches!(
            map_probe(Err(kind.into())),
            Err(crate::reconcile::ReconcileError::Unknown)
        ));
    }
    let mut job = JobFake {
        gone: true,
        active: Ok(true),
        terminated: false,
    };
    assert!(matches!(
        map_probe(probe(&mut job)),
        Err(crate::reconcile::ReconcileError::Unknown)
    ));
    assert!(!job.terminated);
    job.active = Ok(false);
    assert!(map_probe(orphan_empty(&mut job)).unwrap());
    assert!(job.terminated);
    assert!(!map_probe(Ok(false)).unwrap());
}

#[test]
fn absent_with_live_leader_is_unknown() {
    assert!(absent_result(Ok(false)).unwrap());
    assert!(matches!(
        absent_result(Ok(true)),
        Err(crate::reconcile::ReconcileError::Unknown)
    ));
    assert!(absent_result(Err(io::ErrorKind::PermissionDenied.into())).is_err());
}
#[test]
fn exited_leader_empty_job_settles() {
    assert!(acquire_decision(false, Some(false), || Ok(true)).is_ok());
    assert!(acquire_decision(false, None, || Ok(true)).is_ok());
    assert!(acquire_decision(false, Some(false), || Ok(false)).is_err());
    assert!(acquire_decision(false, Some(true), || Ok(true)).is_err());
    assert!(exited_probe(Ok(true)).unwrap());
    assert!(exited_probe(Ok(false)).is_err());
    assert!(exited_probe(Err(io::ErrorKind::PermissionDenied.into())).is_err());
}

#[test]
fn gone_run_id_mismatch_is_unknown() {
    let identity = Proc {
        pid: 42,
        start_time: None,
        group: Some("Local\\pg-run-other".into()),
        pgid: None,
    };
    assert!(scope("run-test", identity.group.as_deref().unwrap()).is_err());
    assert!(matches!(
        crate::reconcile_store::gone("run-test", Some(&identity)),
        Err(crate::reconcile::ReconcileError::Unknown)
    ));
}
