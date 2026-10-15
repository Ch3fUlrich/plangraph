//! The `attempt` activity: heartbeats while it "works".

use crate::AttemptInput;
use std::time::Duration;
use temporalio_macros::activities;
use temporalio_sdk::activities::{ActivityContext, ActivityError};

pub struct CardActivities;

/// Heartbeat period used by the spike (production plan: 30 s).
const BEAT: Duration = Duration::from_millis(250);

#[activities]
impl CardActivities {
    #[activity]
    pub async fn attempt(
        ctx: ActivityContext,
        input: AttemptInput,
    ) -> Result<String, ActivityError> {
        let attempt = ctx.info().attempt;
        // Progress survives a retry through heartbeat details.
        let resumed_from: u32 = ctx.heartbeat_details().deserialize()?.unwrap_or(0);
        match (input.mode.as_str(), attempt) {
            ("fail_first", 1) => {
                return Err(anyhow::anyhow!("simulated crash on attempt 1").into());
            }
            ("stall_first", 1) | ("stall_always", _) => {
                // One heartbeat, then go silent: the server must time us out.
                ctx.record_heartbeat(1u32).await?;
                tokio::select! {
                    _ = tokio::time::sleep(Duration::from_secs(120)) => {}
                    _ = ctx.cancelled() => return Err(ActivityError::cancelled()),
                }
            }
            ("long", _) => loop {
                // Runs until the workflow cancels it; the cancel is delivered
                // to the activity as a flag the next heartbeat round-trip sets.
                ctx.record_heartbeat(0u32).await?;
                tokio::select! {
                    _ = tokio::time::sleep(BEAT) => {}
                    _ = ctx.cancelled() => return Err(ActivityError::cancelled()),
                }
            },
            _ => {}
        }
        for step in resumed_from..input.steps {
            if ctx.is_cancelled() {
                return Err(ActivityError::cancelled());
            }
            ctx.record_heartbeat(step).await?;
            tokio::time::sleep(BEAT).await;
        }
        Ok(format!("done on attempt {attempt}"))
    }
}
