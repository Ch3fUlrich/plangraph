use super::*;
use pg_worker_core::run::record::{RunRecord, Status};
use serde_json::json;
use std::{io, process::ExitStatus};
#[derive(Default)]
struct Fake {
    empty: bool,
    term: usize,
    kill: usize,
}
impl Child for Fake {
    fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn reap(&mut self) -> io::Result<Option<ExitStatus>> {
        Ok(None)
    }
    fn empty(&mut self) -> io::Result<bool> {
        Ok(self.empty)
    }
    fn terminate(&mut self) -> io::Result<()> {
        self.term += 1;
        Ok(())
    }
    fn kill(&mut self) -> io::Result<()> {
        self.kill += 1;
        Ok(())
    }
}
#[test]
fn reconcile_max_s_requires_empty_and_escalates_once() {
    let mut w = Watch::new(
        Role::Implement,
        Limits::default(),
        0,
        (0, Some(1)),
        policy(),
        false,
    );
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 1000), Step::Held));
    assert_eq!(c.term, 1);
    assert!(matches!(w.tick(&mut c, 11_000), Step::Held));
    assert_eq!(c.kill, 1);
    assert!(matches!(w.tick(&mut c, 13_000), Step::Held));
    c.empty = true;
    assert!(matches!(
        w.tick(&mut c, 13_001),
        Step::Ended("killed_max_s")
    ));
}
#[test]
fn reconcile_first_activity_restarts_and_warns_before_stop() {
    let mut w = Watch::new(
        Role::Implement,
        Limits::default(),
        1000,
        (0, None),
        policy(),
        false,
    );
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 145_000), Step::Warn));
    assert_eq!(c.term, 0);
    assert!(matches!(w.tick(&mut c, 184_000), Step::Held));
    assert_eq!(c.term, 1);
}
#[test]
fn reconcile_recorded_activity_uses_tool_max_s() {
    let mut w = Watch::new(
        Role::Implement,
        Limits::default(),
        0,
        (0, None),
        policy(),
        true,
    );
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 700_000), Step::Held));
    assert_eq!(c.term, 0);
    assert!(matches!(w.tick(&mut c, 1_440_000), Step::Warn));
    w.tick(&mut c, 1_803_000);
    assert_eq!(c.term, 1);
}

#[test]
fn reconcile_durable_attachment_and_proven_empty_settlement() {
    for expired in [false, true] {
        let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
        let mut record = RunRecord::create(
            &root,
            "run-test",
            "local",
            json!({"role":"implement","proc":{"pid":123},
                "plan":{"first_leg":{"leg_key":"leg","route_key":"route"}}})
            .as_object()
            .unwrap()
            .clone(),
            Status::Running,
            1.0,
        )
        .unwrap();
        let mut w = Watch::new(
            Role::Implement,
            Limits::default(),
            1000,
            (1000, if expired { Some(1) } else { None }),
            policy(),
            false,
        );
        w.attach(&mut record, 1).unwrap();
        w.attach(&mut record, 1).unwrap();
        let mut c = Fake::default();
        if expired {
            w.advance(&mut c, &root, &mut record, 1, 2000).unwrap();
            assert_eq!(c.term, 1);
            assert_eq!(record.status(), Some(Status::Running));
            w.advance(&mut c, &root, &mut record, 1, 12000).unwrap();
            assert_eq!(c.kill, 1);
        }
        c.empty = true;
        for _ in 0..2 {
            w.advance(&mut c, &root, &mut record, 1, 14000).unwrap();
        }
        let saved = RunRecord::read(&root, "run-test").unwrap();
        assert_eq!(saved.status(), Some(Status::Orphaned));
        assert_eq!(saved.get("exit_code"), Some(&serde_json::Value::Null));
        let events = saved.get("events").unwrap().as_array().unwrap();
        for kind in ["RUN_REATTACHED", "RUN_ORPHANED"] {
            assert_eq!(events.iter().filter(|e| e["event"] == kind).count(), 1);
        }
        assert_eq!(
            saved.get("attempts").unwrap()[0]["how"],
            if expired {
                "killed_max_s"
            } else {
                "ended_unobserved"
            }
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}

fn policy() -> StopPolicy {
    StopPolicy {
        grace_ms: 10_000,
        interval_ms: 2_000,
    }
}

#[test]
fn reconcile_absent_tool_limit_never_invents_silence_kill() {
    let limits = Limits {
        tool_max_s: None,
        ..Limits::default()
    };
    let mut w = Watch::new(Role::Implement, limits, 0, (0, None), policy(), true);
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 9_000_000), Step::Held));
    assert_eq!(c.term, 0);
}

#[test]
fn reconcile_attach_failure_still_enforces_deadline() {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let mut record = RunRecord::create(
        &root,
        "run-test",
        "local",
        json!({"role":"implement"}).as_object().unwrap().clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let mut w = Watch::new(
        Role::Implement,
        Limits::default(),
        0,
        (0, Some(1)),
        policy(),
        false,
    );
    let mut c = Fake::default();
    w.advance(&mut c, &root, &mut record, 9, 1000).unwrap();
    assert_eq!(c.term, 1);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn reconcile_warn_retry_key_and_attach_noop() {
    let root = std::env::temp_dir().join(pg_worker_core::run::record::new_run_id());
    let mut record = RunRecord::create(
        &root,
        "run-test",
        "local",
        json!({"role":"implement","proc":{"pid":123}})
            .as_object()
            .unwrap()
            .clone(),
        Status::Running,
        1.0,
    )
    .unwrap();
    let mut w = Watch::new(
        Role::Implement,
        Limits::default(),
        0,
        (0, None),
        policy(),
        false,
    );
    w.attach(&mut record, 1).unwrap();
    let path = record.path().to_path_buf();
    let bytes = std::fs::read(&path).unwrap();
    std::fs::remove_file(&path).unwrap();
    std::fs::create_dir(&path).unwrap();
    // An already durable marker must not try saving even with an unwritable path.
    w.attach(&mut record, 1).unwrap();
    let mut c = Fake::default();
    for _ in 0..2 {
        assert!(w.advance(&mut c, &root, &mut record, 1, 144_000).is_err());
    }
    assert_eq!(
        record
            .get("events")
            .unwrap()
            .as_array()
            .unwrap()
            .iter()
            .filter(|event| event["event"] == "ACTIVITY_WARN")
            .count(),
        1
    );
    std::fs::remove_dir(&path).unwrap();
    std::fs::write(&path, bytes).unwrap();
    w.advance(&mut c, &root, &mut record, 1, 144_000).unwrap();
    std::fs::remove_dir_all(root).unwrap();
}
