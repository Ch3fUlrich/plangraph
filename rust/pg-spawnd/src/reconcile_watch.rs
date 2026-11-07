//! Clock-driven adopted supervision; no waitpid status or inherited pipes.
use pg_worker_core::{
    activity::{Act, Ctx, Kind, Limits, Mode, Modes, Monitor, Signal},
    hosts::Role,
    watch::{Child, KillProof, Stop},
};

pub(crate) struct Watch {
    monitor: Monitor,
    stop: Option<Stop>,
    how: &'static str,
    deadline: u64,
}
pub(crate) enum Step {
    Held,
    Warn,
    Ended(&'static str),
}
impl Watch {
    pub(crate) fn new(
        role: Role,
        mut limits: Limits,
        restart: u64,
        deadline: u64,
        recorded_activity: bool,
    ) -> Self {
        if recorded_activity {
            limits.silence_s = Some(limits.tool_max_s.unwrap_or(1800));
        }
        let mut monitor = Monitor::new(
            role,
            &limits,
            Modes {
                first: Mode::On,
                silent: Mode::On,
                milestone: Mode::Off,
            },
            restart,
        );
        if recorded_activity {
            // Recovery initialization only; never persist as genuine activity.
            monitor.feed(Signal {
                kind: Kind::Request,
                ts_ms: restart,
                tool: None,
            });
        }
        Self {
            monitor,
            stop: None,
            how: "ended_unobserved",
            deadline,
        }
    }
    pub(crate) fn tick(&mut self, child: &mut dyn Child, now: u64) -> Step {
        if matches!(child.empty(), Ok(true)) {
            return Step::Ended(self.how);
        }
        if self.stop.is_none() {
            if now >= self.deadline {
                self.how = "killed_max_s";
                self.stop = Some(Stop::default());
            } else if let Some(d) = self.monitor.tick(now, Ctx::default()) {
                if d.act == Act::Warn {
                    return Step::Warn;
                }
                if d.act == Act::Kill {
                    self.how = "reattach_timeout";
                    self.stop = Some(Stop::default());
                }
            }
        }
        if let Some(stop) = &mut self.stop {
            if stop.tick(child, now, 10_000, 2_000) == Some(KillProof::Empty) {
                return Step::Ended(self.how);
            }
        }
        Step::Held
    }
}

#[cfg(test)]
#[path = "reconcile_watch_tests.rs"]
mod tests;
