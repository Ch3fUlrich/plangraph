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
    let mut w = Watch::new(Role::Implement, Limits::default(), 0, 100, false);
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 100), Step::Held));
    assert_eq!(c.term, 1);
    assert!(matches!(w.tick(&mut c, 10_100), Step::Held));
    assert_eq!(c.kill, 1);
    assert!(matches!(w.tick(&mut c, 12_100), Step::Held));
    c.empty = true;
    assert!(matches!(
        w.tick(&mut c, 12_101),
        Step::Ended("killed_max_s")
    ));
}
#[test]
fn reconcile_first_activity_restarts_and_warns_before_stop() {
    let mut w = Watch::new(Role::Implement, Limits::default(), 1000, u64::MAX, false);
    let mut c = Fake::default();
    assert!(matches!(w.tick(&mut c, 145_000), Step::Warn));
    assert_eq!(c.term, 0);
    assert!(matches!(w.tick(&mut c, 184_000), Step::Held));
    assert_eq!(c.term, 1);
}
#[test]
fn reconcile_recorded_activity_uses_tool_max_s() {
    let mut w = Watch::new(Role::Implement, Limits::default(), 0, u64::MAX, true);
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
            if expired { 2000 } else { u64::MAX },
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
