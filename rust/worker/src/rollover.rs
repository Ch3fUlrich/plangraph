//! Replay-safe state transferred only between attempts, never during an activity.
use crate::attempt::LegSpec;
use pg_worker_core::budget::{Attempt, ContextClass};
use serde::{Deserialize, Serialize};
use std::time::Duration;

pub const LIMITS_PATCH: &str = "bounded-runs-v1";
pub const EVENT_CAP: u32 = 2_000;
pub const WAIT_CAP: u32 = 100;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Resume {
    pub history: Vec<Attempt>,
    pub base_sha: String,
    pub n: u32,
    pub waits: u32,
    pub waited: Duration,
    pub context: ContextClass,
    pub limited: Vec<String>,
    pub leg: LegSpec,
    pub answers: Vec<String>,
    pub question_rounds: u32,
    pub signals: Vec<crate::workflow::AnswerSignal>,
}

pub fn due(events: u32, waits: u32, suggested: bool) -> bool {
    suggested || events >= EVENT_CAP || waits >= WAIT_CAP
}

/// Bound queue time and the entire retry sequence, not just each execution.
pub fn schedule_to_close(start_to_close: Duration) -> Duration {
    start_to_close
        .saturating_mul(5)
        .saturating_add(Duration::from_secs(60))
}

pub fn bound(opts: &mut temporalio_sdk::ActivityOptions, start: Duration) {
    opts.close_timeouts = temporalio_sdk::ActivityCloseTimeouts::ScheduleAndStartToClose {
        schedule_to_close: schedule_to_close(start),
        start_to_close: start,
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn event_and_wait_caps_roll_over_independently() {
        assert!(!due(EVENT_CAP - 1, WAIT_CAP - 1, false));
        assert!(due(EVENT_CAP, 0, false));
        assert!(due(0, WAIT_CAP, false));
        assert!(due(0, 0, true));
    }

    #[test]
    fn retry_window_includes_queue_and_all_five_attempts() {
        assert_eq!(
            schedule_to_close(Duration::from_secs(2)),
            Duration::from_secs(70)
        );
    }
}
