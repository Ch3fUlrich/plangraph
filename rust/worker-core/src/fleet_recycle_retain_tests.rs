use super::*;
use crate::fleet_owner_lease::{bootstrap, Identity};

#[derive(Clone)]
struct Fake(Observation);
struct DirectLinux(Fake);
struct RemoteShellLinux(Fake);
struct Windows(Fake);
macro_rules! adapter {
    ($ty:ident, $class:ident) => {
        impl RetentionAdapter for $ty {
            fn host_class(&self) -> HostClass {
                HostClass::$class
            }
            fn capabilities(&self) -> Capabilities {
                Capabilities {
                    panes: true,
                    remote_control: true,
                }
            }
            fn observe(&self, session: &str) -> Observation {
                assert!(["session-1", "session-2"].contains(&session));
                self.0 .0.clone()
            }
        }
    };
}
adapter!(DirectLinux, DirectLinux);
adapter!(RemoteShellLinux, RemoteShellLinux);
adapter!(Windows, Windows);

fn retained() -> Fake {
    Fake(Observation {
        transcript: Transcript::Readable,
        pane: Pane::Retained,
        remote_control: RemoteControl::Detached,
        oom_scope: OomScope::Groups([42].into()),
    })
}
fn lease(session: &str) -> Read {
    let mut record = bootstrap(session, "run-1", 10).unwrap();
    record.identity = State::Busy(Identity {
        pid: 41,
        start: "start-1".into(),
        group: 42,
    });
    Read::Known(record)
}
fn report(fake: Fake) -> Report {
    check(
        &DirectLinux(fake),
        &lease("session-1"),
        "session-1",
        "session-2",
        11,
    )
}
#[test]
fn all_host_classes_retain_scoped_evidence() {
    let adapters: Vec<Box<dyn RetentionAdapter>> = vec![
        Box::new(DirectLinux(retained())),
        Box::new(RemoteShellLinux(retained())),
        Box::new(Windows(retained())),
    ];
    for adapter in adapters {
        let r = check(
            adapter.as_ref(),
            &lease("session-1"),
            "session-1",
            "session-2",
            11,
        );
        assert!(r.passed);
        assert!(r.findings.is_empty());
        assert_eq!(r.owner.unwrap().group, 42);
    }
}
#[test]
fn missing_and_gone_are_independent_findings() {
    let mut f = retained();
    f.0.transcript = Transcript::Missing;
    f.0.pane = Pane::Gone;
    let r = report(f);
    assert!(!r.passed);
    assert_eq!(r.findings.len(), 2);
    assert_eq!(r.observation.oom_scope, OomScope::Groups([42].into()));
    assert!(r.findings.iter().all(|f| !f.reason.is_empty()));
}
#[test]
fn every_unknown_alarms_once_and_other_fields_are_evaluated() {
    for field in ["transcript", "pane", "remote_control", "oom_scope"] {
        let mut f = retained();
        match field {
            "transcript" => f.0.transcript = Transcript::Unknown("unavailable".into()),
            "pane" => f.0.pane = Pane::Unknown("unavailable".into()),
            "remote_control" => f.0.remote_control = RemoteControl::Unknown("unavailable".into()),
            _ => f.0.oom_scope = OomScope::Unknown("unavailable".into()),
        }
        let r = report(f.clone());
        assert_eq!(r.observation, f.0);
        assert!(!r.passed);
        assert_eq!(r.findings.len(), 1);
        assert_eq!(r.findings[0].reason, "unavailable");
        assert_eq!(r.findings[0].session_id, "session-1");
        assert!(r.findings[0].alarm);
        if field == "transcript" {
            f.0.pane = Pane::Gone;
        } else {
            f.0.transcript = Transcript::Missing;
        }
        assert_eq!(report(f).findings.len(), 2);
    }
}
#[test]
fn oom_scope_is_exactly_the_owner_group() {
    for groups in [vec![], vec![43], vec![42, 43]] {
        let mut f = retained();
        f.0.oom_scope = OomScope::Groups(groups.into_iter().collect());
        let r = report(f);
        assert!(!r.passed);
        assert_eq!(r.findings[0].field, "oom_scope");
    }
    assert!(report(retained()).passed);
    let mut f = retained();
    f.0.remote_control = RemoteControl::Attached;
    assert!(report(f).passed);
}
#[test]
fn reuse_and_lease_uncertainty_never_pass() {
    let adapter = DirectLinux(retained());
    let r = check(&adapter, &lease("session-1"), "session-1", "session-1", 11);
    assert!(!r.passed);
    let unknown = Read::Unknown {
        session_id: "session-1".into(),
        reason: "unreadable".into(),
        alarm: false,
    };
    let r = check(&adapter, &unknown, "session-1", "session-2", 11);
    assert!(!r.passed);
    assert_eq!(r.findings.len(), 2);
    assert!(r.findings[0].alarm);
    assert_eq!(r.findings[0].session_id, "session-1");
    assert_eq!(r.findings[0].reason, "unreadable");
    assert_eq!(r.findings[1].field, "oom_scope");
    assert!(r.findings[1].alarm);
    assert_eq!(r.findings[1].session_id, "session-1");
    assert!(!r.findings[1].reason.is_empty());
    for groups in [vec![43], vec![42, 43]] {
        let mut f = retained();
        f.0.oom_scope = OomScope::Groups(groups.into_iter().collect());
        let r = check(&DirectLinux(f), &unknown, "session-1", "session-2", 11);
        assert_eq!(r.findings.len(), 2);
        assert_eq!(r.findings[1].field, "oom_scope");
        assert!(r.findings[1].alarm);
    }
    let r = check(&adapter, &lease("session-2"), "session-1", "session-3", 11);
    assert!(!r.passed);
    assert!(r.findings.iter().any(|f| f.field == "lease"));
    let r = check(&adapter, &lease("session-2"), "session-2", "session-3", 11);
    assert!(r.passed);
    for identity in [State::Unknown, State::Free] {
        let Read::Known(mut record) = lease("session-1") else {
            unreachable!()
        };
        record.identity = identity;
        for groups in [vec![42], vec![43], vec![42, 43]] {
            let mut f = retained();
            f.0.oom_scope = OomScope::Groups(groups.into_iter().collect());
            let r = check(
                &DirectLinux(f),
                &Read::Known(record.clone()),
                "session-1",
                "session-2",
                11,
            );
            assert!(!r.passed);
            assert_eq!(r.findings.len(), 2);
            assert_eq!(r.findings[1].field, "oom_scope");
            assert!(r.findings[1].alarm);
        }
    }
}
#[test]
fn not_applicable_requires_declared_capabilities() {
    struct Headless(Fake);
    impl RetentionAdapter for Headless {
        fn host_class(&self) -> HostClass {
            HostClass::DirectLinux
        }
        fn capabilities(&self) -> Capabilities {
            Capabilities {
                panes: false,
                remote_control: false,
            }
        }
        fn observe(&self, _: &str) -> Observation {
            self.0 .0.clone()
        }
    }
    let mut f = retained();
    for remote_control in [RemoteControl::Attached, RemoteControl::Detached] {
        f.0.remote_control = remote_control;
        let r = check(
            &Headless(f.clone()),
            &lease("session-1"),
            "session-1",
            "session-2",
            11,
        );
        assert!(!r.passed);
        assert_eq!(r.findings.len(), 2);
        assert_eq!(r.findings[0].field, "pane");
        assert_eq!(r.findings[1].field, "remote_control");
    }
    f.0.pane = Pane::NotApplicable;
    f.0.remote_control = RemoteControl::NotApplicable;
    assert!(
        check(
            &Headless(f.clone()),
            &lease("session-1"),
            "session-1",
            "session-2",
            11
        )
        .passed
    );
    assert_eq!(report(f.clone()).findings.len(), 2);
    f.0.transcript = Transcript::Missing;
    assert!(
        !check(
            &Headless(f),
            &lease("session-1"),
            "session-1",
            "session-2",
            11
        )
        .passed
    );
}
#[test]
fn report_json_is_evidence_only() {
    let r = report(retained());
    let json = serde_json::to_value(&r).unwrap();
    assert!(json.get("action").is_none());
    assert_eq!(json["observed_at_s"], 11);
    assert_eq!(serde_json::from_value::<Report>(json).unwrap(), r);
}
