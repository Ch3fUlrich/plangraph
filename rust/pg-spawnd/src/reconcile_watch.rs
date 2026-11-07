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
    deadline: Option<u64>,
    policy: StopPolicy,
    warning_pending: bool,
}
pub(crate) struct StopPolicy {
    pub grace_ms: u64,
    pub interval_ms: u64,
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
            if let Err(error) = self.attach(record, attempt) {
                eprintln!("reconciliation attachment failed: {error}");
            }
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
                let key = serde_json::json!([record.get("run_id"), attempt, "ACTIVITY_WARN"]);
                if !record
                    .get("events")
                    .and_then(|v| v.as_array())
                    .is_some_and(|events| events.iter().any(|e| e["idempotency_key"] == key))
                {
                    record
                        .push_event(serde_json::json!({"event":"ACTIVITY_WARN",
                        "attempt":attempt,"reason":"recovery_activity",
                        "idempotency_key":key}))
                        .map_err(|_| crate::reconcile::ReconcileError::Invalid)?;
                }
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
        timing: (u64, Option<u64>),
        policy: StopPolicy,
        recorded_activity: bool,
    ) -> Self {
        if recorded_activity {
            limits.silence_s = limits.tool_max_s;
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
            deadline: crate::reconcile_time::deadline(timing.0, timing.1),
            policy,
            warning_pending: false,
        }
    }
    pub(crate) fn tick(&mut self, child: &mut dyn Child, now: u64) -> Step {
        if matches!(child.empty(), Ok(true)) {
            return Step::Ended(self.how);
        }
        if self.stop.is_none() {
            if self.deadline.is_some_and(|deadline| now >= deadline) {
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
            if stop.tick(child, now, self.policy.grace_ms, self.policy.interval_ms)
                == Some(KillProof::Empty)
            {
                return Step::Ended(self.how);
            }
        }
        Step::Held
    }
}

#[cfg(test)]
#[path = "reconcile_watch_tests.rs"]
mod tests;
