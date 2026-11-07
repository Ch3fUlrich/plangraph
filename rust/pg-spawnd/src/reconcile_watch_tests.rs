use super::*;
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
