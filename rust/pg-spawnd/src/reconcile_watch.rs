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
    warning_pending: bool,
}
pub(crate) enum Step {
    Held,
    Warn,
    Ended(&'static str),
}
impl Watch {
    /// Persist attachment before observing or signalling the adopted child.
    pub(crate) fn attach(
        &self,
        record: &mut pg_worker_core::run::record::RunRecord,
        attempt: u32,
    ) -> Result<(), crate::reconcile::ReconcileError> {
        crate::reconcile_attach::attach(record, attempt)
    }

    /// Caller serializes recovery under the state and record locks. A failed
    /// durable warning prevents an activity-driven signal on this invocation.
    pub(crate) fn advance(
        &mut self,
        child: &mut dyn Child,
        state: &std::path::Path,
        record: &mut pg_worker_core::run::record::RunRecord,
        attempt: u32,
        now: u64,
    ) -> Result<Step, crate::reconcile::ReconcileError> {
        if record.status() == Some(pg_worker_core::run::record::Status::Running) {
            self.attach(record, attempt)?;
        }
        let step = if self.warning_pending {
            Step::Warn
        } else {
            self.tick(child, now)
        };
        match step {
            Step::Ended(how) => {
                crate::reconcile_store::settle(state, record, attempt, how, now)?;
            }
            Step::Warn => {
                self.warning_pending = true;
                record
                    .push_event(serde_json::json!({"event":"ACTIVITY_WARN",
                        "attempt":attempt,"reason":"recovery_activity"}))
                    .map_err(|_| crate::reconcile::ReconcileError::Invalid)?;
                record
                    .save()
                    .map_err(|_| crate::reconcile::ReconcileError::Io)?;
                self.warning_pending = false;
            }
            Step::Held => {}
        }
        Ok(step)
    }

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
            warning_pending: false,
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
