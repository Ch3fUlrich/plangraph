//! Temporal Rust SDK spike: a `card` workflow that runs one long agent
//! attempt as an activity, then waits for an operator answer raced against
//! a durable deadline timer.

pub mod activities;
pub mod card;
pub mod tls;

use serde::{Deserialize, Serialize};

/// Task queue used by the worker and the driver.
pub const TASK_QUEUE: &str = "card-spike";

/// Input of the `card` workflow.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CardInput {
    /// Activity behaviour: ok | fail_first | stall_first | stall_always | long
    pub mode: String,
    /// Retry policy maximum attempts for the activity (0 = unlimited).
    pub max_attempts: u32,
    /// Question deadline; when it expires the default answer is taken.
    pub deadline_ms: u64,
}

/// Input of the `attempt` activity.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AttemptInput {
    pub mode: String,
    /// Number of heartbeat steps to do before returning.
    pub steps: u32,
}

/// Result of the `card` workflow.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CardOutput {
    /// What the activity phase produced (or how it failed).
    pub attempt: String,
    /// How the question phase resolved: `answer:<text>`, `default`, `cancelled`.
    pub resolution: String,
}
